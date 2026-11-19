from django.contrib import admin, messages

from . import webhooks
from .models import Merchant, Order, Refund, WebhookEvent


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "api_key_prefix", "webhook_url", "created_at"]
    search_fields = ["name", "owner__email"]
    readonly_fields = ["api_key_prefix", "api_key_hash", "webhook_secret"]


class RefundInline(admin.TabularInline):
    model = Refund
    extra = 0
    can_delete = False
    readonly_fields = ["amount", "reason", "reference", "created_by", "created_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["created_at", "merchant", "description", "amount", "refunded_amount", "status", "customer"]
    list_filter = ["status", "merchant"]
    search_fields = ["id", "merchant_reference", "customer__email"]
    inlines = [RefundInline]
    # Money-related fields only change through merchants/services.py.
    readonly_fields = ["status", "amount", "refunded_amount", "customer", "payment_reference", "paid_at"]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "merchant", "event_type", "status", "attempts", "last_status_code", "last_error"]
    list_filter = ["status", "event_type"]
    actions = ["retry_now"]

    @admin.action(description="Retry delivery now")
    def retry_now(self, request, queryset):
        ids = list(queryset.exclude(status=WebhookEvent.Status.DELIVERED).values_list("id", flat=True))
        WebhookEvent.objects.filter(id__in=ids).update(status=WebhookEvent.Status.PENDING, attempts=0)
        for event_id in ids:
            webhooks.deliver(event_id)
        messages.info(request, f"Tried {len(ids)} event(s).")
