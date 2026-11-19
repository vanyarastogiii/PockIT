from django.contrib import admin, messages

from . import services
from .models import IdempotencyKey, LedgerEntry, PaymentHold, PaymentRequest, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["__str__", "kind", "balance", "updated_at"]
    list_filter = ["kind"]
    search_fields = ["user__email", "name"]
    readonly_fields = ["balance"]  # balances only change through services.py


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ["created_at", "wallet", "entry_type", "amount", "balance_after", "reference"]
    list_filter = ["entry_type"]
    search_fields = ["wallet__user__email", "reference"]

    # The ledger is append-only: no editing or deleting history.
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ["created_at", "requester", "payer", "amount", "status", "resolved_at"]
    list_filter = ["status"]
    search_fields = ["requester__email", "payer__email", "note"]
    # Status changes must go through services.py (locks + money movement).
    readonly_fields = ["status", "transfer_reference", "resolved_at"]


@admin.register(PaymentHold)
class PaymentHoldAdmin(admin.ModelAdmin):
    """The risk review queue: approve sends the money on, reject returns it."""

    list_display = ["created_at", "kind", "sender", "recipient", "amount", "status", "why"]
    list_filter = ["status", "kind"]
    search_fields = ["sender__email", "recipient__email"]
    readonly_fields = [f.name for f in PaymentHold._meta.fields]
    actions = ["approve", "reject"]

    @admin.display(description="Why it was held")
    def why(self, obj):
        return "; ".join(reason["message"] for reason in obj.reasons)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _review(self, request, queryset, action, label):
        done = 0
        for hold in queryset.filter(status=PaymentHold.Status.HELD):
            try:
                action(hold.id, reviewer=request.user, note=f"{label} in admin")
                done += 1
            except services.WalletError as exc:
                messages.error(request, f"Hold #{hold.id}: {exc.detail}")
        messages.success(request, f"{label} {done} payment(s).")

    @admin.action(description="Approve: send the money to the recipient")
    def approve(self, request, queryset):
        self._review(request, queryset, services.release_hold, "Approved")

    @admin.action(description="Reject: return the money to the sender")
    def reject(self, request, queryset):
        self._review(request, queryset, services.reject_hold, "Rejected")


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ["created_at", "user", "endpoint", "key", "response_code"]
    search_fields = ["user__email", "key"]
