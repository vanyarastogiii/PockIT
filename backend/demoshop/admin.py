from django.contrib import admin

from .models import ProcessedWebhook, ShopOrder


@admin.register(ShopOrder)
class ShopOrderAdmin(admin.ModelAdmin):
    list_display = ["id", "product_name", "amount", "status", "refunded_amount", "created_at"]
    list_filter = ["status"]


@admin.register(ProcessedWebhook)
class ProcessedWebhookAdmin(admin.ModelAdmin):
    list_display = ["received_at", "event_type", "event_id"]
