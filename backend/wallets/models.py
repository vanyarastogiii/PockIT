"""
The tables that do the real work:

  Wallet          one per user (plus internal SYSTEM wallets), holds a balance
  LedgerEntry     one row per movement of money (the audit trail)
  PaymentRequest  "Priya asks Rahul for ₹200"
  PaymentHold     a payment stopped by the risk rules, waiting for review
  IdempotencyKey  remembers the result of each payment attempt so a retry
                  or double click returns the same result instead of paying twice

Money is stored as Decimal (never float!) with 2 decimal places.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class Wallet(models.Model):
    class Kind(models.TextChoices):
        USER = "USER", "User"
        # Internal wallets that belong to PockIt itself, e.g. "risk_holds", which
        # keeps held payments safe while they wait for review. Using a real wallet
        # (instead of a flag) keeps the double-entry ledger balanced at all times.
        SYSTEM = "SYSTEM", "System"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.USER)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet", null=True, blank=True
    )
    name = models.CharField(max_length=50, blank=True, default="")  # only for SYSTEM wallets
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Last line of defence: the DATABASE itself refuses a negative balance,
            # even if a bug in our Python code tries to save one.
            models.CheckConstraint(
                condition=models.Q(balance__gte=0), name="wallet_balance_non_negative"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="USER", user__isnull=False)
                    | models.Q(kind="SYSTEM", user__isnull=True)
                ),
                name="wallet_user_xor_system",
            ),
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(kind="SYSTEM"), name="unique_system_wallet_name"
            ),
        ]

    def __str__(self):
        if self.kind == self.Kind.SYSTEM:
            return f"[system] {self.name}: ₹{self.balance}"
        return f"{self.user.email}: ₹{self.balance}"


class LedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        TOPUP = "TOPUP", "Top-up"
        TRANSFER_OUT = "TRANSFER_OUT", "Sent"
        TRANSFER_IN = "TRANSFER_IN", "Received"
        PAYMENT_OUT = "PAYMENT_OUT", "Paid a merchant"
        PAYMENT_IN = "PAYMENT_IN", "Payment from a customer"
        REFUND_OUT = "REFUND_OUT", "Refund given"
        REFUND_IN = "REFUND_IN", "Refund received"
        HOLD_IN = "HOLD_IN", "Held for review"            # system wallet only
        HOLD_RELEASE = "HOLD_RELEASE", "Released from hold"  # system wallet only
        HOLD_RETURN = "HOLD_RETURN", "Returned after review"

    # Types where money leaves a user's wallet to pay someone (used by the risk rules).
    OUTGOING_PAYMENT_TYPES = (EntryType.TRANSFER_OUT, EntryType.PAYMENT_OUT)

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="entries")
    # Signed: negative = money out, positive = money in.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    # The other party of the business transaction (null for top-ups). For a held
    # payment this is still the real recipient, even though the money sits in
    # the system holding wallet until review.
    counterparty = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # All rows of one payment share the same reference.
    reference = models.UUIDField(default=uuid.uuid4, db_index=True)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    # Optional message from the sender, e.g. "Dinner at Saravana Bhavan".
    note = models.CharField(max_length=140, blank=True, default="")
    hold = models.ForeignKey(
        "PaymentHold", on_delete=models.PROTECT, null=True, blank=True, related_name="entries"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["wallet", "-created_at"])]
        verbose_name_plural = "ledger entries"

    def __str__(self):
        return f"{self.entry_type} {self.amount} ({self.wallet})"


class PaymentRequest(models.Model):
    """
    "Priya asks Rahul for ₹200."  A small state machine:

        PENDING ──pay──────▶ PAID
           │ │ └─pay (risky)─▶ ON_HOLD ──approve──▶ PAID
           │ │                        └─reject───▶ PENDING (money returned)
           │ ├──decline───▶ DECLINED   (by the payer)
           │ └──cancel────▶ CANCELLED  (by the requester)

    The row is locked while it changes, so a request can never be paid twice,
    or paid after being cancelled.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ON_HOLD = "ON_HOLD", "On hold"
        PAID = "PAID", "Paid"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requests_sent"
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requests_received"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=140, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Links to the ledger rows once paid.
    transfer_reference = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["payer", "status"]),
            models.Index(fields=["requester", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="request_amount_positive"),
            models.CheckConstraint(
                condition=~models.Q(requester=models.F("payer")), name="request_not_to_self"
            ),
        ]

    def __str__(self):
        return f"{self.requester.email} asks {self.payer.email} for ₹{self.amount} ({self.status})"


class PaymentHold(models.Model):
    """
    A payment the risk rules stopped for review.

        HELD ──approve──▶ RELEASED  (money goes on to the recipient)
          └───reject───▶ REJECTED  (money goes back to the sender)

    While HELD, the money sits in the SYSTEM "risk_holds" wallet: it has left
    the sender, but the recipient can't spend it yet.
    """

    class Kind(models.TextChoices):
        TRANSFER = "TRANSFER", "Transfer"
        REQUEST = "REQUEST", "Request payment"
        ORDER = "ORDER", "Checkout payment"

    class Status(models.TextChoices):
        HELD = "HELD", "Held"
        RELEASED = "RELEASED", "Released"
        REJECTED = "REJECTED", "Rejected"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.HELD)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=140, blank=True, default="")
    reference = models.UUIDField(default=uuid.uuid4, db_index=True)
    # Which rules fired, e.g. [{"code": "new_recipient_large", "message": "..."}]
    reasons = models.JSONField(default=list)
    payment_request = models.ForeignKey(
        PaymentRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="holds"
    )
    order = models.ForeignKey(
        "merchants.Order", on_delete=models.PROTECT, null=True, blank=True, related_name="holds"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    review_note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "-created_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="hold_amount_positive"),
        ]

    def __str__(self):
        return f"Hold #{self.id}: {self.sender.email} → {self.recipient.email} ₹{self.amount} ({self.status})"


class IdempotencyKey(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=64)
    endpoint = models.CharField(max_length=50)
    # Fingerprint of the request body, so the same key can't be reused
    # for a *different* payment.
    request_hash = models.CharField(max_length=64)
    response_code = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_idempotency_key_per_user"),
        ]

    def __str__(self):
        return f"{self.user.email} {self.endpoint} {self.key}"
