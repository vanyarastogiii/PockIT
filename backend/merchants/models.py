"""
"Pay with PockIt": the merchant side.

  Merchant      a PockIt user who accepts payments; has a secret API key
  Order         one checkout: created by the merchant, paid by a customer
  Refund        money going back from merchant to customer (full or partial)
  WebhookEvent  a message to the merchant's server ("order.paid"), stored in
                the SAME database transaction as the payment (transactional
                outbox), then delivered with retries
"""
import hashlib
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


class Merchant(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="merchant")
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80, unique=True)
    # We never store the API key itself, only its SHA-256 hash (like a password).
    # The prefix lets the merchant recognise which key is which: "sk_test_Ab3x…".
    api_key_prefix = models.CharField(max_length=16)
    api_key_hash = models.CharField(max_length=64, unique=True)
    webhook_url = models.URLField(max_length=500, blank=True, default="")
    webhook_secret = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @staticmethod
    def new_api_key():
        """Returns (raw_key, prefix, hash). Show raw_key to the merchant ONCE."""
        raw = "sk_test_" + secrets.token_urlsafe(24)
        return raw, raw[:12], hash_api_key(raw)

    @staticmethod
    def new_webhook_secret():
        return "whsec_" + secrets.token_urlsafe(24)


def default_expiry():
    return timezone.now() + timedelta(minutes=30)


class Order(models.Model):
    """
        CREATED ──pay──▶ PAID ──refund──▶ PARTIALLY_REFUNDED ──refund──▶ REFUNDED
           │       └─pay (risky)─▶ ON_HOLD ──approve──▶ PAID
           │                              └─reject───▶ DECLINED
           └── (30 min pass) ──▶ EXPIRED
    """

    class Status(models.TextChoices):
        CREATED = "CREATED", "Waiting for payment"
        ON_HOLD = "ON_HOLD", "On hold"
        PAID = "PAID", "Paid"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        REFUNDED = "REFUNDED", "Refunded"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"

    REFUNDABLE = (Status.PAID, Status.PARTIALLY_REFUNDED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="orders")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    description = models.CharField(max_length=140)
    merchant_reference = models.CharField(max_length=64, blank=True, default="")  # the shop's own order id
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="orders_paid"
    )
    refunded_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    payment_reference = models.UUIDField(null=True, blank=True)
    return_url = models.CharField(max_length=500, blank=True, default="")
    cancel_url = models.CharField(max_length=500, blank=True, default="")
    expires_at = models.DateTimeField(default=default_expiry)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["merchant", "-created_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="order_amount_positive"),
            # The database itself refuses to refund more than was paid.
            models.CheckConstraint(
                condition=models.Q(refunded_amount__gte=0) & models.Q(refunded_amount__lte=models.F("amount")),
                name="order_refund_within_amount",
            ),
        ]

    def __str__(self):
        return f"{self.merchant.name} ₹{self.amount} ({self.status})"

    @property
    def refundable_amount(self):
        return self.amount - self.refunded_amount if self.status in self.REFUNDABLE else Decimal("0")


class Refund(models.Model):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.CharField(max_length=140, blank=True, default="")
    reference = models.UUIDField(default=uuid.uuid4, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="refund_amount_positive")]

    def __str__(self):
        return f"Refund ₹{self.amount} on {self.order_id}"


class WebhookEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed (gave up)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # also the event id
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="webhook_events")
    event_type = models.CharField(max_length=40)  # order.paid, order.on_hold, order.declined, refund.created
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.event_type} → {self.merchant.name} ({self.status})"
