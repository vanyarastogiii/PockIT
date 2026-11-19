"""
The demo shop's "server". Three endpoints:

  POST /shop/api/buy                 customer clicks "Pay with PockIt"
  GET  /shop/api/orders/<id>         the shop page polls its own order status
  POST /shop/webhooks/pockit         PockIt tells the shop what happened

The shop marks an order PAID only when a correctly signed webhook says so.
The redirect back from checkout just shows "confirming your payment...":
a customer could fake a redirect, but not PockIt's signature.
"""
import json
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema

from merchants import services as pockit
from merchants import webhooks
from merchants.models import Merchant

from .models import PRODUCTS, ProcessedWebhook, ShopOrder

MERCHANT_SLUG = "chai-and-co"

# Webhook order statuses mapped to the shop's own, with a rank so a late,
# retried webhook can never move an order backwards (e.g. refunded -> paid).
STATUS_MAP = {
    "ON_HOLD": (ShopOrder.Status.ON_HOLD, 1),
    "PAID": (ShopOrder.Status.PAID, 2),
    "DECLINED": (ShopOrder.Status.DECLINED, 2),
    "PARTIALLY_REFUNDED": (ShopOrder.Status.PARTIALLY_REFUNDED, 3),
    "REFUNDED": (ShopOrder.Status.REFUNDED, 4),
}
RANK = {ShopOrder.Status.WAITING: 0, **{status: rank for status, rank in STATUS_MAP.values()}}


def shop_merchant():
    return Merchant.objects.filter(slug=MERCHANT_SLUG).first()


def shop_order_json(order):
    return {
        "id": order.id,
        "product": order.product,
        "product_name": order.product_name,
        "amount": str(order.amount),
        "status": order.status,
        "refunded_amount": str(order.refunded_amount),
    }


class BuySerializer(serializers.Serializer):
    product = serializers.ChoiceField(choices=list(PRODUCTS))


class BuyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "checkout"

    @extend_schema(summary="Demo shop: start a purchase", request=BuySerializer, tags=["Demo shop"], auth=[],
                   responses={201: None})
    def post(self, request):
        merchant = shop_merchant()
        if merchant is None:
            return Response({"detail": "The demo shop isn't set up. Run: python manage.py seed_demo"}, status=503)
        serializer = BuySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_code = serializer.validated_data["product"]
        product = PRODUCTS[product_code]

        shop_order = ShopOrder.objects.create(
            product=product_code, product_name=product["name"], amount=Decimal(product["price"])
        )
        # A real shop's server would call  POST /api/v1/orders  with its secret
        # API key (see api.http). This demo lives in the same Django project, so
        # it calls the same service function directly.
        order = pockit.create_order(
            merchant,
            amount=shop_order.amount,
            description=f"{product['name']} from Chai & Co.",
            merchant_reference=f"chai-{shop_order.id}",
            return_url=f"/shop/?order={shop_order.id}",
            cancel_url=f"/shop/?cancelled={shop_order.id}",
        )
        shop_order.pockit_order_id = order.id
        shop_order.save(update_fields=["pockit_order_id"])
        return Response(
            {"shop_order_id": shop_order.id, "checkout_url": f"/checkout.html?order={order.id}"}, status=201
        )


class ShopOrderView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(summary="Demo shop: order status", tags=["Demo shop"], auth=[], responses={200: None})
    def get(self, request, order_id):
        order = ShopOrder.objects.filter(id=order_id).first()
        if order is None:
            return Response({"detail": "Order not found."}, status=404)
        return Response(shop_order_json(order))


@csrf_exempt
@require_POST
def pockit_webhook(request):
    merchant = shop_merchant()
    if merchant is None:
        return JsonResponse({"detail": "Shop not set up."}, status=503)

    # 1. Is this really from PockIt? Check the signature on the RAW body.
    signature = request.headers.get(webhooks.SIGNATURE_HEADER, "")
    if not webhooks.verify(merchant.webhook_secret, request.body, signature):
        return JsonResponse({"detail": "Bad signature."}, status=400)

    event = json.loads(request.body)
    data = event.get("data", {})

    with transaction.atomic():
        # 2. Have we handled this event already? (Webhooks can be delivered twice.)
        try:
            with transaction.atomic():
                ProcessedWebhook.objects.create(event_id=event["id"], event_type=event["type"])
        except IntegrityError:
            return JsonResponse({"ok": True, "duplicate": True})

        # 3. Update our own order, never moving it backwards.
        order = ShopOrder.objects.select_for_update().filter(pockit_order_id=data.get("order_id")).first()
        if order is None:
            return JsonResponse({"ok": True, "ignored": "unknown order"})
        new_status, new_rank = STATUS_MAP.get(data.get("status"), (None, -1))
        if new_status and new_rank >= RANK[order.status]:
            order.status = new_status
        order.refunded_amount = max(order.refunded_amount, Decimal(data.get("refunded_amount", "0")))
        order.save(update_fields=["status", "refunded_amount", "updated_at"])
    return JsonResponse({"ok": True})
