"""
All money movement lives here, NOT in the views.

Every operation that moves money follows the same recipe:

  1. Check the risk rules (read-only, before taking any locks)
  2. Open a database transaction          -> all or nothing (atomicity)
  3. Lock the row(s) FOR UPDATE           -> concurrent requests wait their turn
  4. Check the idempotency key            -> a retry returns the first result
  5. Validate (e.g. enough balance)
  6. Move the money and write ledger rows (or put it on hold for review)
  7. Save the idempotency key with the response
  8. Commit

Because step 4 runs while the sender's wallet is locked, two copies of the
same request can never both get past it: the second one waits for the lock,
then finds the key the first one saved.

Lock order is always: payment request / order row -> hold row -> wallets by
ascending id (the system holding wallet included). Using one fixed order
everywhere is what makes deadlocks impossible.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from . import risk
from .models import IdempotencyKey, LedgerEntry, PaymentHold, PaymentRequest, Wallet

User = get_user_model()
T = LedgerEntry.EntryType
HOLDS_WALLET_NAME = "risk_holds"


# ------------------------------------------------------------------ errors
class WalletError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Wallet operation failed."
    default_code = "wallet_error"


class InsufficientFunds(WalletError):
    default_detail = "Insufficient balance."
    default_code = "insufficient_funds"


class SelfTransfer(WalletError):
    default_detail = "You cannot send money to yourself."
    default_code = "self_transfer"


class SelfRequest(WalletError):
    default_detail = "You cannot request money from yourself."
    default_code = "self_request"


class RecipientNotFound(WalletError):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = "No user found with that email."
    default_code = "recipient_not_found"


class RequestNotFound(WalletError):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = "Payment request not found."
    default_code = "request_not_found"


class RequestNotPending(WalletError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This request has already been paid, declined or cancelled."
    default_code = "request_not_pending"


class HoldNotHeld(WalletError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This hold has already been reviewed."
    default_code = "hold_already_reviewed"


class IdempotencyConflict(WalletError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "This Idempotency-Key was already used for a different request."
    default_code = "idempotency_conflict"


# ------------------------------------------------------------------ idempotency
@dataclass
class Result:
    status_code: int
    body: dict
    replayed: bool = False  # True when we returned a saved result instead of acting


def request_hash(endpoint, payload):
    raw = json.dumps({"endpoint": endpoint, **payload}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def find_previous(user, key, endpoint, req_hash):
    """Return the saved Result for this key, or None if it's a new request."""
    saved = IdempotencyKey.objects.filter(user=user, key=key).first()
    if saved is None:
        return None
    if saved.endpoint != endpoint or saved.request_hash != req_hash:
        raise IdempotencyConflict()
    return Result(saved.response_code, saved.response_body, replayed=True)


def remember(user, key, endpoint, req_hash, result):
    IdempotencyKey.objects.create(
        user=user,
        key=key,
        endpoint=endpoint,
        request_hash=req_hash,
        response_code=result.status_code,
        response_body=result.body,
    )


# ------------------------------------------------------------------ ledger primitives
def holds_wallet_id() -> int:
    wallet, _ = Wallet.objects.get_or_create(kind=Wallet.Kind.SYSTEM, name=HOLDS_WALLET_NAME)
    return wallet.id


def lock_wallets(*wallet_ids) -> dict:
    """Lock these wallets, always in ascending id order. Returns {wallet_id: wallet}."""
    wallets = Wallet.objects.select_for_update().filter(id__in=set(wallet_ids)).order_by("id")
    return {w.id: w for w in wallets}


def post_entries(debit, credit, amount, *, debit_type, credit_type, debit_cp, credit_cp,
                 reference=None, note="", hold=None):
    """
    Move `amount` from `debit` wallet to `credit` wallet and write the two
    ledger rows (they always sum to zero). The caller MUST already hold locks
    on both wallets inside a transaction.

    debit_cp / credit_cp: the wallet each row should show as "the other party".
    Usually that's simply the other wallet, but for held payments it's the real
    sender/recipient rather than the internal holding wallet.
    """
    if debit.balance < amount:
        raise InsufficientFunds()
    reference = reference or uuid.uuid4()

    debit.balance -= amount
    credit.balance += amount
    debit.save(update_fields=["balance", "updated_at"])
    credit.save(update_fields=["balance", "updated_at"])

    debit_entry = LedgerEntry.objects.create(
        wallet=debit, amount=-amount, entry_type=debit_type, counterparty=debit_cp,
        reference=reference, balance_after=debit.balance, note=note, hold=hold,
    )
    LedgerEntry.objects.create(
        wallet=credit, amount=amount, entry_type=credit_type, counterparty=credit_cp,
        reference=reference, balance_after=credit.balance, note=note, hold=hold,
    )
    return debit_entry


def send_or_hold(*, sender, sender_wallet, recipient, recipient_wallet, amount, note, reasons,
                 out_type, in_type, hold_kind, **hold_links):
    """
    Pay the recipient, or (if the risk rules fired) move the money into the
    holding wallet and create a PaymentHold. Caller holds all wallet locks.
    Returns (hold_or_None, debit_entry).
    """
    if not reasons:
        entry = post_entries(
            sender_wallet, recipient_wallet, amount,
            debit_type=out_type, credit_type=in_type,
            debit_cp=recipient_wallet, credit_cp=sender_wallet, note=note,
        )
        return None, entry

    if sender_wallet.balance < amount:
        raise InsufficientFunds()  # check before creating the hold row
    holds_id = holds_wallet_id()
    holds = lock_wallets(holds_id)[holds_id]  # the caller already locked it; this just fetches it
    hold = PaymentHold.objects.create(
        kind=hold_kind, sender=sender, recipient=recipient, amount=amount, note=note,
        reasons=[r.as_dict() for r in reasons], **hold_links,
    )
    entry = post_entries(
        sender_wallet, holds, amount,
        debit_type=out_type, credit_type=T.HOLD_IN,
        debit_cp=recipient_wallet, credit_cp=sender_wallet,
        reference=hold.reference, note=note, hold=hold,
    )
    return hold, entry


def hold_body(hold):
    return {
        "status": "ON_HOLD",
        "hold_id": hold.id,
        "reasons": hold.reasons,
        "message": "This payment is on hold for a quick security review. "
                   "The money has left your balance and will either reach the recipient "
                   "or come back to you after review.",
    }


# ------------------------------------------------------------------ top-up
def top_up(user, amount: Decimal, idempotency_key: str) -> Result:
    endpoint = "topup"
    req_hash = request_hash(endpoint, {"amount": str(amount)})

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(user=user)

        previous = find_previous(user, idempotency_key, endpoint, req_hash)
        if previous:
            return previous

        wallet.balance += amount
        wallet.save(update_fields=["balance", "updated_at"])

        entry = LedgerEntry.objects.create(
            wallet=wallet,
            amount=amount,
            entry_type=T.TOPUP,
            balance_after=wallet.balance,
        )

        result = Result(
            status.HTTP_201_CREATED,
            {
                "reference": str(entry.reference),
                "amount": str(amount),
                "balance": str(wallet.balance),
                "created_at": entry.created_at.isoformat(),
            },
        )
        remember(user, idempotency_key, endpoint, req_hash, result)
        return result


# ------------------------------------------------------------------ transfer
def _find_user(email):
    return User.objects.filter(email__iexact=email.strip(), is_active=True).first()


def transfer(sender, recipient_email: str, amount: Decimal, idempotency_key: str, note: str = "") -> Result:
    endpoint = "transfer"
    recipient_email = recipient_email.strip().lower()
    req_hash = request_hash(endpoint, {"to": recipient_email, "amount": str(amount), "note": note})

    if recipient_email == sender.email.lower():
        raise SelfTransfer()

    recipient = _find_user(recipient_email)
    if recipient is None:
        raise RecipientNotFound()

    sender_wallet = Wallet.objects.get(user=sender)
    recipient_wallet = Wallet.objects.get(user=recipient)
    # Risk is read-only, so it runs before we take locks. (Two simultaneous
    # payments may both see "4 recent payments"; that's acceptable for a soft
    # velocity rule, and it keeps lock times short.)
    reasons = risk.assess(sender, sender_wallet, recipient_wallet, amount)

    with transaction.atomic():
        ids = [sender_wallet.id, recipient_wallet.id] + ([holds_wallet_id()] if reasons else [])
        wallets = lock_wallets(*ids)
        sender_wallet, recipient_wallet = wallets[sender_wallet.id], wallets[recipient_wallet.id]

        previous = find_previous(sender, idempotency_key, endpoint, req_hash)
        if previous:
            return previous

        hold, entry = send_or_hold(
            sender=sender, sender_wallet=sender_wallet,
            recipient=recipient, recipient_wallet=recipient_wallet,
            amount=amount, note=note, reasons=reasons,
            out_type=T.TRANSFER_OUT, in_type=T.TRANSFER_IN, hold_kind=PaymentHold.Kind.TRANSFER,
        )

        body = {
            "status": "COMPLETED",
            "reference": str(entry.reference),
            "amount": str(amount),
            "to": recipient.email,
            "to_name": recipient.first_name,
            "note": note,
            "balance": str(sender_wallet.balance),
            "created_at": entry.created_at.isoformat(),
        }
        if hold:
            body.update(hold_body(hold))
        result = Result(status.HTTP_202_ACCEPTED if hold else status.HTTP_201_CREATED, body)
        remember(sender, idempotency_key, endpoint, req_hash, result)
        return result


# ------------------------------------------------------------------ payment requests
def create_request(requester, payer_email: str, amount: Decimal, note: str = "") -> PaymentRequest:
    payer_email = payer_email.strip().lower()
    if payer_email == requester.email.lower():
        raise SelfRequest()
    payer = _find_user(payer_email)
    if payer is None:
        raise RecipientNotFound()
    # No money moves yet, so no locking or idempotency key is needed here.
    return PaymentRequest.objects.create(requester=requester, payer=payer, amount=amount, note=note)


def pay_request(payer, request_id: int, idempotency_key: str) -> Result:
    endpoint = "pay_request"
    req_hash = request_hash(endpoint, {"request_id": request_id})

    with transaction.atomic():
        # 1. Lock the request row. A second "Pay" click (even from another tab,
        #    with a different idempotency key) waits here, then sees it's no longer PENDING.
        payment_request = (
            PaymentRequest.objects.select_for_update()
            .filter(id=request_id, payer=payer)
            .select_related("requester")
            .first()
        )
        if payment_request is None:
            raise RequestNotFound()
        requester = payment_request.requester

        payer_wallet = Wallet.objects.get(user=payer)
        requester_wallet = Wallet.objects.get(user=requester)
        reasons = risk.assess(payer, payer_wallet, requester_wallet, payment_request.amount)

        # 2. Then lock the wallets (same order rule as transfers).
        ids = [payer_wallet.id, requester_wallet.id] + ([holds_wallet_id()] if reasons else [])
        wallets = lock_wallets(*ids)
        payer_wallet, requester_wallet = wallets[payer_wallet.id], wallets[requester_wallet.id]

        previous = find_previous(payer, idempotency_key, endpoint, req_hash)
        if previous:
            return previous

        if payment_request.status != PaymentRequest.Status.PENDING:
            raise RequestNotPending()

        hold, entry = send_or_hold(
            sender=payer, sender_wallet=payer_wallet,
            recipient=requester, recipient_wallet=requester_wallet,
            amount=payment_request.amount, note=payment_request.note, reasons=reasons,
            out_type=T.TRANSFER_OUT, in_type=T.TRANSFER_IN, hold_kind=PaymentHold.Kind.REQUEST,
            payment_request=payment_request,
        )

        if hold:
            payment_request.status = PaymentRequest.Status.ON_HOLD
            payment_request.save(update_fields=["status"])
        else:
            payment_request.status = PaymentRequest.Status.PAID
            payment_request.transfer_reference = entry.reference
            payment_request.resolved_at = timezone.now()
            payment_request.save(update_fields=["status", "transfer_reference", "resolved_at"])

        body = {
            "request_id": payment_request.id,
            "status": payment_request.status,
            "reference": str(entry.reference),
            "amount": str(payment_request.amount),
            "to": requester.email,
            "to_name": requester.first_name,
            "balance": str(payer_wallet.balance),
        }
        if hold:
            body.update(hold_body(hold))
        result = Result(status.HTTP_202_ACCEPTED if hold else status.HTTP_200_OK, body)
        remember(payer, idempotency_key, endpoint, req_hash, result)
        return result


def _close_request(user, request_id, *, as_role, new_status):
    """Decline (payer) or cancel (requester) a pending request."""
    with transaction.atomic():
        payment_request = (
            PaymentRequest.objects.select_for_update().filter(id=request_id, **{as_role: user}).first()
        )
        if payment_request is None:
            raise RequestNotFound()
        if payment_request.status == new_status:
            return payment_request  # already done: safe to call twice
        if payment_request.status != PaymentRequest.Status.PENDING:
            raise RequestNotPending()
        payment_request.status = new_status
        payment_request.resolved_at = timezone.now()
        payment_request.save(update_fields=["status", "resolved_at"])
        return payment_request


def decline_request(payer, request_id: int) -> PaymentRequest:
    return _close_request(payer, request_id, as_role="payer", new_status=PaymentRequest.Status.DECLINED)


def cancel_request(requester, request_id: int) -> PaymentRequest:
    return _close_request(
        requester, request_id, as_role="requester", new_status=PaymentRequest.Status.CANCELLED
    )


# ------------------------------------------------------------------ hold review
def _lock_hold_and_parent(hold_id):
    """Lock order: parent (request/order) row first, then the hold row."""
    from merchants.models import Order  # avoid a circular import

    peek = PaymentHold.objects.filter(id=hold_id).values("payment_request_id", "order_id").first()
    if peek is None:
        raise WalletError("Hold not found.")
    parent = None
    if peek["payment_request_id"]:
        parent = PaymentRequest.objects.select_for_update().get(id=peek["payment_request_id"])
    elif peek["order_id"]:
        parent = Order.objects.select_for_update().select_related("merchant").get(id=peek["order_id"])
    hold = PaymentHold.objects.select_for_update().select_related("sender", "recipient").get(id=hold_id)
    return hold, parent


def release_hold(hold_id: int, reviewer=None, note: str = "") -> PaymentHold:
    """Approve a held payment: the money goes on to the recipient."""
    from merchants import webhooks

    with transaction.atomic():
        hold, parent = _lock_hold_and_parent(hold_id)
        if hold.status == PaymentHold.Status.RELEASED:
            return hold  # already approved: safe to call twice
        if hold.status != PaymentHold.Status.HELD:
            raise HoldNotHeld()

        sender_wallet = Wallet.objects.get(user=hold.sender)
        recipient_wallet = Wallet.objects.get(user=hold.recipient)
        wallets = lock_wallets(holds_wallet_id(), sender_wallet.id, recipient_wallet.id)
        in_type = T.PAYMENT_IN if hold.kind == PaymentHold.Kind.ORDER else T.TRANSFER_IN
        post_entries(
            wallets[holds_wallet_id()], wallets[recipient_wallet.id], hold.amount,
            debit_type=T.HOLD_RELEASE, credit_type=in_type,
            debit_cp=wallets[recipient_wallet.id], credit_cp=wallets[sender_wallet.id],
            reference=hold.reference, note=hold.note, hold=hold,
        )

        now = timezone.now()
        hold.status = PaymentHold.Status.RELEASED
        hold.reviewed_by, hold.review_note, hold.resolved_at = reviewer, note, now
        hold.save(update_fields=["status", "reviewed_by", "review_note", "resolved_at"])

        if hold.kind == PaymentHold.Kind.REQUEST:
            parent.status = PaymentRequest.Status.PAID
            parent.transfer_reference, parent.resolved_at = hold.reference, now
            parent.save(update_fields=["status", "transfer_reference", "resolved_at"])
        elif hold.kind == PaymentHold.Kind.ORDER:
            parent.status = parent.Status.PAID
            parent.paid_at, parent.payment_reference = now, hold.reference
            parent.save(update_fields=["status", "paid_at", "payment_reference"])
            webhooks.enqueue(parent.merchant, "order.paid", webhooks.order_payload(parent))
        return hold


def reject_hold(hold_id: int, reviewer=None, note: str = "") -> PaymentHold:
    """Reject a held payment: the money goes back to the sender."""
    from merchants import webhooks

    with transaction.atomic():
        hold, parent = _lock_hold_and_parent(hold_id)
        if hold.status == PaymentHold.Status.REJECTED:
            return hold
        if hold.status != PaymentHold.Status.HELD:
            raise HoldNotHeld()

        sender_wallet = Wallet.objects.get(user=hold.sender)
        recipient_wallet = Wallet.objects.get(user=hold.recipient)
        wallets = lock_wallets(holds_wallet_id(), sender_wallet.id, recipient_wallet.id)
        post_entries(
            wallets[holds_wallet_id()], wallets[sender_wallet.id], hold.amount,
            debit_type=T.HOLD_RELEASE, credit_type=T.HOLD_RETURN,
            debit_cp=wallets[sender_wallet.id], credit_cp=wallets[recipient_wallet.id],
            reference=hold.reference, note=hold.note, hold=hold,
        )

        now = timezone.now()
        hold.status = PaymentHold.Status.REJECTED
        hold.reviewed_by, hold.review_note, hold.resolved_at = reviewer, note, now
        hold.save(update_fields=["status", "reviewed_by", "review_note", "resolved_at"])

        if hold.kind == PaymentHold.Kind.REQUEST:
            parent.status = PaymentRequest.Status.PENDING  # the payer can try again or decline
            parent.save(update_fields=["status"])
        elif hold.kind == PaymentHold.Kind.ORDER:
            parent.status = parent.Status.DECLINED
            parent.save(update_fields=["status"])
            webhooks.enqueue(parent.merchant, "order.declined", webhooks.order_payload(parent))
        return hold


# Note: only successful results are remembered. If a request fails (say,
# insufficient balance), the transaction rolls back, nothing is saved, and the
# client may retry with the same key after topping up. Stripe also stores
# failures; that's a design choice you can discuss in an interview.
