"""
Merchant money movement: checkout payments and refunds.

Same recipe as wallets/services.py (risk -> transaction -> locks ->
idempotency -> validate -> move money -> remember), reusing its ledger
primitives so there is still exactly ONE place where balances change.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status

from wallets import risk
from wallets.models import LedgerEntry, PaymentHold, Wallet
from wallets.services import (
    InsufficientFunds,
    Result,
    WalletError,
    find_previous,
    hold_body,
    holds_wallet_id,
    lock_wallets,
    post_entries,
    remember,
    request_hash,
    send_or_hold,
)

from . import webhooks
from .models import Merchant, Order, Refund

T = LedgerEntry.EntryType


# ------------------------------------------------------------------ errors
class AlreadyMerchant(WalletError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "You already accept payments with PockIt."
    default_code = "already_merchant"


class OrderNotFound(WalletError):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = "Order not found."
    default_code = "order_not_found"


class OrderNotPayable(WalletError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This order can't be paid."
    default_code = "order_not_payable"


class PayOwnShop(WalletError):
    default_detail = "You can't pay your own shop."
    default_code = "pay_own_shop"


class OrderNotRefundable(WalletError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Only paid orders can be refunded."
    default_code = "order_not_refundable"


class RefundTooLarge(WalletError):
    default_detail = "That's more than can be refunded on this order."
    default_code = "refund_too_large"


NOT_PAYABLE_MESSAGES = {
    Order.Status.PAID: "This order has already been paid.",
    Order.Status.PARTIALLY_REFUNDED: "This order has already been paid.",
    Order.Status.REFUNDED: "This order has already been paid and refunded.",
    Order.Status.ON_HOLD: "A payment for this order is already being reviewed.",
    Order.Status.DECLINED: "A payment for this order was declined. Ask the shop for a new checkout link.",
    Order.Status.EXPIRED: "This checkout link has expired. Go back to the shop and try again.",
}


# ------------------------------------------------------------------ merchants
def create_merchant(owner, name: str, webhook_url: str = ""):
    """Returns (merchant, raw_api_key). The raw key is shown to the merchant once."""
    if Merchant.objects.filter(owner=owner).exists():
        raise AlreadyMerchant()
    raw_key, prefix, key_hash = Merchant.new_api_key()
    base = slugify(name)[:60] or "shop"
    for attempt in range(5):
        slug = base if attempt == 0 else f"{base}-{owner.id}-{attempt}"
        try:
            with transaction.atomic():
                merchant = Merchant.objects.create(
                    owner=owner, name=name, slug=slug, api_key_prefix=prefix, api_key_hash=key_hash,
                    webhook_url=webhook_url, webhook_secret=Merchant.new_webhook_secret(),
                )
            return merchant, raw_key
        except IntegrityError:
            if Merchant.objects.filter(owner=owner).exists():
                raise AlreadyMerchant()
    raise WalletError("Couldn't create the merchant, try another name.")


def rotate_api_key(merchant) -> str:
    raw_key, prefix, key_hash = Merchant.new_api_key()
    merchant.api_key_prefix, merchant.api_key_hash = prefix, key_hash
    merchant.save(update_fields=["api_key_prefix", "api_key_hash"])
    return raw_key


# ------------------------------------------------------------------ orders
def create_order(merchant, amount: Decimal, description: str, merchant_reference: str = "",
                 return_url: str = "", cancel_url: str = "") -> Order:
    # No money moves when an order is created, so no locks are needed.
    return Order.objects.create(
        merchant=merchant, amount=amount, description=description,
        merchant_reference=merchant_reference, return_url=return_url, cancel_url=cancel_url,
    )


def is_expired(order) -> bool:
    return order.status == Order.Status.CREATED and order.expires_at <= timezone.now()


def get_order_for_checkout(order_id) -> Order:
    order = Order.objects.select_related("merchant").filter(id=order_id).first()
    if order is None:
        raise OrderNotFound()
    if is_expired(order):
        # A conditional UPDATE, so it can't overwrite a payment that just happened.
        Order.objects.filter(id=order.id, status=Order.Status.CREATED).update(status=Order.Status.EXPIRED)
        order.refresh_from_db()
    return order


def pay_order(customer, order_id, idempotency_key: str) -> Result:
    endpoint = "pay_order"
    req_hash = request_hash(endpoint, {"order_id": str(order_id)})

    with transaction.atomic():
        # Lock the order first: two tabs paying the same checkout queue up here,
        # and the second one sees it's no longer CREATED.
        order = (
            Order.objects.select_for_update(of=("self",))
            .select_related("merchant__owner")
            .filter(id=order_id)
            .first()
        )
        if order is None:
            raise OrderNotFound()
        merchant = order.merchant
        if merchant.owner_id == customer.id:
            raise PayOwnShop()

        customer_wallet = Wallet.objects.get(user=customer)
        merchant_wallet = Wallet.objects.get(user=merchant.owner)
        reasons = risk.assess(customer, customer_wallet, merchant_wallet, order.amount)

        ids = [customer_wallet.id, merchant_wallet.id] + ([holds_wallet_id()] if reasons else [])
        wallets = lock_wallets(*ids)
        customer_wallet, merchant_wallet = wallets[customer_wallet.id], wallets[merchant_wallet.id]

        previous = find_previous(customer, idempotency_key, endpoint, req_hash)
        if previous:
            return previous

        if is_expired(order):
            raise OrderNotPayable(NOT_PAYABLE_MESSAGES[Order.Status.EXPIRED])
        if order.status != Order.Status.CREATED:
            raise OrderNotPayable(NOT_PAYABLE_MESSAGES.get(order.status))

        hold, entry = send_or_hold(
            sender=customer, sender_wallet=customer_wallet,
            recipient=merchant.owner, recipient_wallet=merchant_wallet,
            amount=order.amount, note=order.description, reasons=reasons,
            out_type=T.PAYMENT_OUT, in_type=T.PAYMENT_IN, hold_kind=PaymentHold.Kind.ORDER,
            order=order,
        )

        order.customer = customer
        if hold:
            order.status = Order.Status.ON_HOLD
            order.save(update_fields=["customer", "status"])
            webhooks.enqueue(merchant, "order.on_hold", webhooks.order_payload(order))
        else:
            order.status = Order.Status.PAID
            order.paid_at, order.payment_reference = timezone.now(), entry.reference
            order.save(update_fields=["customer", "status", "paid_at", "payment_reference"])
            webhooks.enqueue(merchant, "order.paid", webhooks.order_payload(order))

        body = {
            "order_id": str(order.id),
            "status": order.status,
            "amount": str(order.amount),
            "merchant_name": merchant.name,
            "return_url": order.return_url,
            "reference": str(entry.reference),
            "balance": str(customer_wallet.balance),
        }
        if hold:
            body.update(hold_body(hold))
        result = Result(status.HTTP_202_ACCEPTED if hold else status.HTTP_200_OK, body)
        remember(customer, idempotency_key, endpoint, req_hash, result)
        return result


# ------------------------------------------------------------------ refunds
def refund_order(merchant, order_id, amount: Decimal, reason: str, idempotency_key: str, created_by=None) -> Result:
    """
    Refund all or part of a paid order. No risk checks: the money can only go
    back to the customer who paid, and never more than they paid.
    """
    endpoint = "refund"
    req_hash = request_hash(endpoint, {"order_id": str(order_id), "amount": str(amount), "reason": reason})
    owner = merchant.owner

    with transaction.atomic():
        order = (
            # of=("self",): lock just the order row; the customer join is optional (outer).
            Order.objects.select_for_update(of=("self",)).select_related("customer")
            .filter(id=order_id, merchant=merchant).first()
        )
        if order is None:
            raise OrderNotFound()
        if order.customer_id is None:
            raise OrderNotRefundable()

        merchant_wallet = Wallet.objects.get(user=owner)
        customer_wallet = Wallet.objects.get(user=order.customer)
        wallets = lock_wallets(merchant_wallet.id, customer_wallet.id)
        merchant_wallet, customer_wallet = wallets[merchant_wallet.id], wallets[customer_wallet.id]

        previous = find_previous(owner, idempotency_key, endpoint, req_hash)
        if previous:
            return previous

        if order.status not in Order.REFUNDABLE:
            raise OrderNotRefundable()
        remaining = order.amount - order.refunded_amount
        if amount > remaining:
            raise RefundTooLarge(f"You can refund at most ₹{remaining} on this order.")

        refund = Refund.objects.create(order=order, amount=amount, reason=reason, created_by=created_by)
        try:
            post_entries(
                merchant_wallet, customer_wallet, amount,
                debit_type=T.REFUND_OUT, credit_type=T.REFUND_IN,
                debit_cp=customer_wallet, credit_cp=merchant_wallet,
                reference=refund.reference, note=reason or order.description,
            )
        except InsufficientFunds:
            raise InsufficientFunds("Your PockIt balance is too low to give this refund.")

        order.refunded_amount += amount
        order.status = Order.Status.REFUNDED if order.refunded_amount == order.amount else Order.Status.PARTIALLY_REFUNDED
        order.save(update_fields=["refunded_amount", "status"])

        webhooks.enqueue(
            merchant, "refund.created",
            {**webhooks.order_payload(order), "refund": {"id": refund.id, "amount": str(amount), "reason": reason}},
        )

        result = Result(
            status.HTTP_201_CREATED,
            {
                "refund_id": refund.id,
                "order_id": str(order.id),
                "amount": str(amount),
                "refunded_amount": str(order.refunded_amount),
                "order_status": order.status,
                "merchant_balance": str(merchant_wallet.balance),
            },
        )
        remember(owner, idempotency_key, endpoint, req_hash, result)
        return result
