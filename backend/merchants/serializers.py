import ipaddress
import socket
from decimal import Decimal
from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers

from wallets.serializers import MIN_AMOUNT

from .models import Merchant, Order, Refund, WebhookEvent
from .services import is_expired


def _is_public_host(host: str) -> bool:
    """Block webhooks to localhost / private networks in production (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def validate_redirect_url(value: str) -> str:
    """Absolute http(s) URL, or a path on this site like /shop/thanks."""
    if not value:
        return value
    if value.startswith("/") and not value.startswith("//"):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise serializers.ValidationError("Use a full http(s) URL or a path starting with /.")
    return value


class MerchantCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    webhook_url = serializers.URLField(max_length=500, required=False, allow_blank=True, default="")

    def validate_webhook_url(self, value):
        return validate_webhook_url(value)


def validate_webhook_url(value):
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise serializers.ValidationError("Use an http or https URL.")
    if not settings.DEBUG and not _is_public_host(parsed.hostname or ""):
        raise serializers.ValidationError("Webhook URLs must be publicly reachable.")
    return value


class MerchantUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80, required=False)
    webhook_url = serializers.URLField(max_length=500, required=False, allow_blank=True)

    def validate_webhook_url(self, value):
        return validate_webhook_url(value)


class MerchantSerializer(serializers.ModelSerializer):
    api_key_hint = serializers.SerializerMethodField()
    stats = serializers.SerializerMethodField()

    class Meta:
        model = Merchant
        fields = ["name", "slug", "api_key_hint", "webhook_url", "webhook_secret", "created_at", "stats"]

    def get_api_key_hint(self, obj) -> str:
        return f"{obj.api_key_prefix}…"

    def get_stats(self, obj) -> dict:
        orders = obj.orders.all()
        paid = [o for o in orders if o.status in (Order.Status.PAID, Order.Status.PARTIALLY_REFUNDED, Order.Status.REFUNDED)]
        return {
            "orders_paid": len(paid),
            "total_received": str(sum((o.amount for o in paid), Decimal("0"))),
            "total_refunded": str(sum((o.refunded_amount for o in paid), Decimal("0"))),
            "on_hold": sum(1 for o in orders if o.status == Order.Status.ON_HOLD),
        }


class OrderCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("1.00"), max_value=Decimal(settings.MAX_ORDER_AMOUNT)
    )
    description = serializers.CharField(max_length=140)
    merchant_reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    return_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    cancel_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_return_url(self, value):
        return validate_redirect_url(value)

    def validate_cancel_url(self, value):
        return validate_redirect_url(value)


class RefundCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_AMOUNT)
    reason = serializers.CharField(max_length=140, required=False, allow_blank=True, default="")


class RefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = ["id", "amount", "reason", "created_at"]


class OrderSerializer(serializers.ModelSerializer):
    """The merchant's view of an order."""

    status = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    checkout_url = serializers.SerializerMethodField()
    refunds = RefundSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id", "status", "amount", "refunded_amount", "currency", "description", "merchant_reference",
            "customer_name", "customer_email", "checkout_url", "return_url", "cancel_url",
            "created_at", "paid_at", "expires_at", "refunds",
        ]

    def get_status(self, obj) -> str:
        return Order.Status.EXPIRED if is_expired(obj) else obj.status

    def get_customer_name(self, obj) -> str | None:
        return obj.customer.first_name if obj.customer_id else None

    def get_customer_email(self, obj) -> str | None:
        return obj.customer.email if obj.customer_id else None

    def get_checkout_url(self, obj) -> str:
        path = f"/checkout.html?order={obj.id}"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class CheckoutOrderSerializer(serializers.ModelSerializer):
    """What the customer sees on the checkout page. No customer data."""

    merchant_name = serializers.CharField(source="merchant.name")
    status = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "merchant_name", "amount", "currency", "description", "status",
                  "return_url", "cancel_url", "expires_at"]

    def get_status(self, obj) -> str:
        return Order.Status.EXPIRED if is_expired(obj) else obj.status


class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = ["id", "event_type", "status", "attempts", "last_status_code", "last_error",
                  "next_attempt_at", "created_at", "delivered_at"]
