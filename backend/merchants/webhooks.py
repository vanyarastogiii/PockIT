"""
Webhooks: telling the merchant's server what happened ("order.paid").

Transactional outbox
--------------------
`enqueue()` only INSERTS a WebhookEvent row, inside the same database
transaction that moved the money. So either both happen or neither does:
we can never take a customer's money and forget to tell the merchant, or
tell the merchant about a payment that was rolled back.

Delivery
--------
After the transaction commits, we try to deliver straight away (in a
background thread, so the customer isn't kept waiting). If the merchant's
server is down or returns an error, the event stays PENDING and is retried
with exponential backoff (30 s, 1 min, 2 min, 4 min, ...) by
`python manage.py deliver_webhooks`, then marked FAILED after N attempts.

Signing
-------
Every request carries   PockIt-Signature: t=<unix time>,v1=<hex HMAC-SHA256>
computed over "<t>.<raw body>" with the merchant's webhook secret (the same
scheme Stripe uses). The merchant recomputes it to prove the message really
came from PockIt and wasn't replayed later (they should reject old t values).
"""
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import WebhookEvent

log = logging.getLogger(__name__)
SIGNATURE_HEADER = "PockIt-Signature"
EVENT_ID_HEADER = "PockIt-Event-Id"
SIGNATURE_TOLERANCE_SECONDS = 300


# ------------------------------------------------------------------ payloads
def order_payload(order):
    return {
        "order_id": str(order.id),
        "merchant_reference": order.merchant_reference,
        "status": order.status,
        "amount": str(order.amount),
        "refunded_amount": str(order.refunded_amount),
        "currency": order.currency,
        "description": order.description,
        "customer_email": order.customer.email if order.customer_id else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }


# ------------------------------------------------------------------ signing
def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    timestamp = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(secret: str, body: bytes, header: str, tolerance=SIGNATURE_TOLERANCE_SECONDS) -> bool:
    """What a merchant runs on their side. Constant-time compare + replay window."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        expected = sign(secret, body, timestamp).split("v1=")[1]
        fresh = abs(time.time() - timestamp) <= tolerance
        return fresh and hmac.compare_digest(expected, parts["v1"])
    except (KeyError, ValueError, AttributeError):
        return False


# ------------------------------------------------------------------ outbox
def enqueue(merchant, event_type: str, data: dict) -> WebhookEvent:
    """Call INSIDE the money transaction. Delivery starts after commit."""
    event = WebhookEvent.objects.create(
        merchant=merchant,
        event_type=event_type,
        payload={"type": event_type, "created": timezone.now().isoformat(), "data": data},
    )
    if merchant.webhook_url:
        transaction.on_commit(lambda: _deliver_soon(event.id))
    return event


def _deliver_soon(event_id):
    if not getattr(settings, "WEBHOOK_DELIVER_IN_BACKGROUND", True):
        return  # tests and the worker command deliver explicitly

    def run():
        try:
            deliver(event_id)
        finally:
            close_old_connections()

    threading.Thread(target=run, daemon=True).start()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=30 * (2 ** max(attempts - 1, 0)))


def deliver(event_id, opener=urllib.request.urlopen) -> WebhookEvent:
    """
    Try to deliver one event. Three short steps, so no database transaction
    stays open while we wait on the merchant's server:

      1. claim:  lock the row, bump `attempts` and push `next_attempt_at` out
                 (a lease, so the retry worker won't pick it up meanwhile)
      2. send:   the HTTP POST, outside any transaction
      3. record: DELIVERED, or schedule the next retry, or FAILED
    """
    with transaction.atomic():
        event = (
            WebhookEvent.objects.select_for_update(skip_locked=True)
            .select_related("merchant")
            .filter(id=event_id, status=WebhookEvent.Status.PENDING)
            .first()
        )
        if event is None:
            return None  # already delivered, given up, or someone else is sending it
        merchant = event.merchant
        if not merchant.webhook_url:
            event.last_error = "No webhook URL configured."
            event.next_attempt_at = timezone.now() + timedelta(hours=1)
            event.save(update_fields=["last_error", "next_attempt_at"])
            return event
        event.attempts += 1
        event.next_attempt_at = timezone.now() + timedelta(seconds=settings.WEBHOOK_TIMEOUT_SECONDS * 3)
        event.save(update_fields=["attempts", "next_attempt_at"])

    body = json.dumps({"id": str(event.id), **event.payload}, separators=(",", ":")).encode()
    request = urllib.request.Request(
        merchant.webhook_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PockIt-Webhooks/1.0",
            SIGNATURE_HEADER: sign(merchant.webhook_secret, body),
            EVENT_ID_HEADER: str(event.id),
        },
    )
    try:
        with opener(request, timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as response:
            code = response.status
        ok, error = 200 <= code < 300, "" if 200 <= code < 300 else f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        code, ok, error = exc.code, False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001  (timeouts, refused connections, DNS...)
        code, ok, error = None, False, str(exc)[:300]

    event.last_status_code = code
    if ok:
        event.status, event.delivered_at, event.last_error = WebhookEvent.Status.DELIVERED, timezone.now(), ""
    elif event.attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
        event.status, event.last_error = WebhookEvent.Status.FAILED, error
    else:
        event.last_error = error
        event.next_attempt_at = timezone.now() + _backoff(event.attempts)
    event.save(update_fields=["status", "delivered_at", "last_error", "last_status_code", "next_attempt_at"])
    if not ok:
        log.warning("Webhook %s to %s failed: %s", event.id, merchant.webhook_url, error)
    return event


def deliver_due(limit=50, opener=urllib.request.urlopen) -> int:
    """Deliver every PENDING event whose retry time has come. Returns how many were tried."""
    due = list(
        WebhookEvent.objects.filter(status=WebhookEvent.Status.PENDING, next_attempt_at__lte=timezone.now())
        .order_by("next_attempt_at")
        .values_list("id", flat=True)[:limit]
    )
    for event_id in due:
        deliver(event_id, opener=opener)
    return len(due)
