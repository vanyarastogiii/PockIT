from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from wallets.views import IDEMPOTENCY_PARAM, get_idempotency_key, to_response

from . import services, webhooks
from .auth import MerchantKeyAuthentication
from .models import Merchant, Order, WebhookEvent
from .serializers import (
    CheckoutOrderSerializer,
    MerchantCreateSerializer,
    MerchantSerializer,
    MerchantUpdateSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    RefundCreateSerializer,
    WebhookEventSerializer,
)

PAY_RESPONSES = {
    200: OpenApiResponse(description="Paid"),
    202: OpenApiResponse(description="On hold for a security review"),
    400: OpenApiResponse(description="Insufficient balance, or paying your own shop"),
    404: OpenApiResponse(description="Order not found"),
    409: OpenApiResponse(description="Already paid, on hold, declined or expired"),
}
REFUND_RESPONSES = {
    201: OpenApiResponse(description="Refunded"),
    400: OpenApiResponse(description="More than the refundable amount, or merchant balance too low"),
    404: OpenApiResponse(description="Order not found"),
    409: OpenApiResponse(description="Order isn't paid"),
}


def merchant_for(user) -> Merchant:
    merchant = Merchant.objects.filter(owner=user).first()
    if merchant is None:
        raise NotFound("You don't accept payments yet.")
    return merchant


# ================================================================== Merchant API (secret key)
class MerchantAPIView(APIView):
    """Server-to-server API: the shop's backend calls these with its secret key."""

    authentication_classes = [MerchantKeyAuthentication]
    permission_classes = [IsAuthenticated]

    @property
    def merchant(self) -> Merchant:
        return self.request.auth


class ApiOrderCreateView(MerchantAPIView):
    @extend_schema(
        summary="Create a checkout order",
        description="Returns a `checkout_url`. Send your customer there to pay with PockIt.",
        request=OrderCreateSerializer, responses={201: OrderSerializer}, tags=["Merchant API (secret key)"],
    )
    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = services.create_order(self.merchant, **serializer.validated_data)
        return Response(OrderSerializer(order, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ApiOrderDetailView(MerchantAPIView):
    @extend_schema(summary="Get an order", responses=OrderSerializer, tags=["Merchant API (secret key)"])
    def get(self, request, order_id):
        order = Order.objects.filter(id=order_id, merchant=self.merchant).select_related("customer").first()
        if order is None:
            raise NotFound("Order not found.")
        return Response(OrderSerializer(order, context={"request": request}).data)


class ApiRefundView(MerchantAPIView):
    @extend_schema(
        summary="Refund all or part of an order",
        request=RefundCreateSerializer, parameters=[IDEMPOTENCY_PARAM], responses=REFUND_RESPONSES,
        tags=["Merchant API (secret key)"],
    )
    def post(self, request, order_id):
        key = get_idempotency_key(request)
        serializer = RefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return to_response(services.refund_order(
            self.merchant, order_id, serializer.validated_data["amount"], serializer.validated_data["reason"],
            key, created_by=request.user,
        ))


# ================================================================== Business page (logged-in owner)
class MyMerchantView(APIView):
    @extend_schema(summary="Your merchant profile", responses=MerchantSerializer, tags=["Business"])
    def get(self, request):
        return Response(MerchantSerializer(merchant_for(request.user)).data)

    @extend_schema(
        summary="Start accepting payments",
        description="Creates your merchant account. The response contains your secret `api_key` ONCE: save it.",
        request=MerchantCreateSerializer, responses={201: OpenApiResponse(description="Merchant and API key")},
        tags=["Business"],
    )
    def post(self, request):
        serializer = MerchantCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        merchant, raw_key = services.create_merchant(request.user, **serializer.validated_data)
        return Response({**MerchantSerializer(merchant).data, "api_key": raw_key}, status=status.HTTP_201_CREATED)

    @extend_schema(summary="Update name or webhook URL", request=MerchantUpdateSerializer,
                   responses=MerchantSerializer, tags=["Business"])
    def patch(self, request):
        merchant = merchant_for(request.user)
        serializer = MerchantUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(merchant, field, value)
        merchant.save()
        return Response(MerchantSerializer(merchant).data)


class RotateKeyView(APIView):
    @extend_schema(summary="Replace your secret API key", request=None,
                   responses={200: OpenApiResponse(description="The new key, shown once")}, tags=["Business"])
    def post(self, request):
        raw_key = services.rotate_api_key(merchant_for(request.user))
        return Response({"api_key": raw_key})


@extend_schema(summary="Your shop's orders", tags=["Business"])
class MyOrdersView(generics.ListAPIView):
    serializer_class = OrderSerializer

    def get_queryset(self):
        return (
            Order.objects.filter(merchant=merchant_for(self.request.user))
            # Checkouts nobody paid (abandoned or expired) aren't interesting here.
            .exclude(status__in=[Order.Status.CREATED, Order.Status.EXPIRED])
            .select_related("customer").prefetch_related("refunds").order_by("-created_at")
        )


class MyRefundView(APIView):
    @extend_schema(summary="Refund an order from the Business page", request=RefundCreateSerializer,
                   parameters=[IDEMPOTENCY_PARAM], responses=REFUND_RESPONSES, tags=["Business"])
    def post(self, request, order_id):
        key = get_idempotency_key(request)
        serializer = RefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return to_response(services.refund_order(
            merchant_for(request.user), order_id, serializer.validated_data["amount"],
            serializer.validated_data["reason"], key, created_by=request.user,
        ))


@extend_schema(summary="Recent webhook deliveries", tags=["Business"])
class MyWebhookEventsView(generics.ListAPIView):
    serializer_class = WebhookEventSerializer

    def get_queryset(self):
        return WebhookEvent.objects.filter(merchant=merchant_for(self.request.user)).order_by("-created_at")


class RetryWebhookView(APIView):
    @extend_schema(summary="Send a webhook again", request=None, responses=WebhookEventSerializer, tags=["Business"])
    def post(self, request, event_id):
        merchant = merchant_for(request.user)
        updated = WebhookEvent.objects.filter(id=event_id, merchant=merchant).exclude(
            status=WebhookEvent.Status.DELIVERED
        ).update(status=WebhookEvent.Status.PENDING, attempts=0)
        event = WebhookEvent.objects.filter(id=event_id, merchant=merchant).first()
        if event is None:
            raise NotFound("Event not found.")
        if updated:
            webhooks.deliver(event.id)
            event.refresh_from_db()
        return Response(WebhookEventSerializer(event).data)


# ================================================================== Checkout (customer)
class CheckoutOrderView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(summary="Checkout page: what am I paying for?", responses=CheckoutOrderSerializer,
                   tags=["Checkout"], auth=[])
    def get(self, request, order_id):
        return Response(CheckoutOrderSerializer(services.get_order_for_checkout(order_id)).data)


class CheckoutPayView(APIView):
    throttle_scope = "checkout"

    @extend_schema(summary="Pay a checkout order", request=None, parameters=[IDEMPOTENCY_PARAM],
                   responses=PAY_RESPONSES, tags=["Checkout"])
    def post(self, request, order_id):
        key = get_idempotency_key(request)
        return to_response(services.pay_order(request.user, order_id, key))
