from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from .models import LedgerEntry, PaymentRequest, Wallet

MIN_AMOUNT = Decimal("0.01")


def format_inr(amount) -> str:
    """₹ with Indian digit grouping: 1234567.5 -> ₹12,34,567.50"""
    amount = Decimal(amount).quantize(Decimal("0.01"))
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if amount < 0 else ''}₹{whole}.{frac}"


# ------------------------------------------------------------------ input
class TopUpSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=MIN_AMOUNT,
        max_value=Decimal(settings.MAX_TOPUP_AMOUNT),
    )


class TransferSerializer(serializers.Serializer):
    to_email = serializers.EmailField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT)
    note = serializers.CharField(max_length=140, required=False, allow_blank=True, default="")

    def validate_note(self, value):
        return value.strip()


class PaymentRequestCreateSerializer(serializers.Serializer):
    from_email = serializers.EmailField(help_text="Who should pay you")
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT)
    note = serializers.CharField(max_length=140, required=False, allow_blank=True, default="")

    def validate_note(self, value):
        return value.strip()


# ------------------------------------------------------------------ output
class WalletSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.first_name")
    email = serializers.EmailField(source="user.email")

    class Meta:
        model = Wallet
        fields = ["name", "email", "balance", "updated_at"]


def display_name(wallet, business=False) -> str:
    """Name to show for the other party. Merchants show their shop name."""
    if wallet is None or wallet.user is None:
        return "PockIt"
    user = wallet.user
    if business:
        merchant = getattr(user, "merchant", None)
        if merchant is not None:
            return merchant.name
    return user.first_name or user.email


class LedgerEntrySerializer(serializers.ModelSerializer):
    description = serializers.SerializerMethodField()
    hold_status = serializers.SerializerMethodField(
        help_text="For a payment stopped by the risk rules: HELD, RELEASED or REJECTED"
    )

    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "reference",
            "entry_type",
            "amount",
            "balance_after",
            "description",
            "note",
            "hold_status",
            "created_at",
        ]

    def get_description(self, obj) -> str:
        T = LedgerEntry.EntryType
        amount = format_inr(abs(obj.amount))
        if obj.entry_type == T.TOPUP:
            return f"Added {amount} to wallet"
        if obj.entry_type == T.TRANSFER_OUT:
            return f"Sent {amount} to {display_name(obj.counterparty)}"
        if obj.entry_type == T.TRANSFER_IN:
            return f"Received {amount} from {display_name(obj.counterparty)}"
        if obj.entry_type == T.PAYMENT_OUT:
            return f"Paid {amount} to {display_name(obj.counterparty, business=True)}"
        if obj.entry_type == T.PAYMENT_IN:
            return f"Payment of {amount} from {display_name(obj.counterparty)}"
        if obj.entry_type == T.REFUND_OUT:
            return f"Refunded {amount} to {display_name(obj.counterparty)}"
        if obj.entry_type == T.REFUND_IN:
            return f"Refund of {amount} from {display_name(obj.counterparty, business=True)}"
        if obj.entry_type == T.HOLD_RETURN:
            return f"Returned {amount}: payment to {display_name(obj.counterparty, business=True)} wasn't approved"
        return obj.get_entry_type_display()

    def get_hold_status(self, obj) -> str | None:
        return obj.hold.status if obj.hold_id and obj.amount < 0 else None


class PaymentRequestSerializer(serializers.ModelSerializer):
    """A request as seen by the logged-in user (they may be the requester or the payer)."""

    direction = serializers.SerializerMethodField(help_text="incoming = you are asked to pay")
    other_name = serializers.SerializerMethodField()
    other_email = serializers.SerializerMethodField()

    class Meta:
        model = PaymentRequest
        fields = [
            "id",
            "direction",
            "other_name",
            "other_email",
            "amount",
            "note",
            "status",
            "created_at",
            "resolved_at",
        ]

    def _me(self):
        return self.context["request"].user

    def _other(self, obj):
        return obj.requester if obj.payer_id == self._me().id else obj.payer

    def get_direction(self, obj) -> str:
        return "incoming" if obj.payer_id == self._me().id else "outgoing"

    def get_other_name(self, obj) -> str:
        return self._other(obj).first_name

    def get_other_email(self, obj) -> str:
        return self._other(obj).email
