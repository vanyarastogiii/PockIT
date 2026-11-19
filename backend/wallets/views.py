from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import LedgerEntry, PaymentRequest
from .serializers import (
    LedgerEntrySerializer,
    PaymentRequestCreateSerializer,
    PaymentRequestSerializer,
    TopUpSerializer,
    TransferSerializer,
    WalletSerializer,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"

IDEMPOTENCY_PARAM = OpenApiParameter(
    name=IDEMPOTENCY_HEADER,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "A unique id for this payment attempt (e.g. a UUID). Sending the same key "
        "again returns the original result instead of moving money twice."
    ),
)


def get_idempotency_key(request):
    key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not key:
        raise ValidationError({IDEMPOTENCY_HEADER: "This header is required."})
    if len(key) > 64:
        raise ValidationError({IDEMPOTENCY_HEADER: "Must be 64 characters or fewer."})
    return key


def to_response(result: services.Result):
    response = Response(result.body, status=result.status_code)
    if result.replayed:
        # Lets the client (and you, while testing) see that nothing was charged again.
        response["Idempotent-Replayed"] = "true"
    return response


# ------------------------------------------------------------------ wallet
class WalletView(APIView):
    @extend_schema(summary="Your balance", responses=WalletSerializer, tags=["Wallet"])
    def get(self, request):
        return Response(WalletSerializer(request.user.wallet).data)


class TopUpView(APIView):
    @extend_schema(
        summary="Add money (simulated top-up)",
        request=TopUpSerializer,
        parameters=[IDEMPOTENCY_PARAM],
        responses={201: OpenApiResponse(description="Money added")},
        tags=["Wallet"],
    )
    def post(self, request):
        key = get_idempotency_key(request)
        serializer = TopUpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.top_up(request.user, serializer.validated_data["amount"], key)
        return to_response(result)


class TransferView(APIView):
    throttle_scope = "transfers"  # rate limit, see REST_FRAMEWORK in settings.py

    @extend_schema(
        summary="Send money to another user",
        request=TransferSerializer,
        parameters=[IDEMPOTENCY_PARAM],
        responses={
            201: OpenApiResponse(description="Money sent"),
            202: OpenApiResponse(description="On hold for a security review (risk rules)"),
            400: OpenApiResponse(description="Insufficient balance or invalid input"),
            404: OpenApiResponse(description="No user with that email"),
            429: OpenApiResponse(description="Too many transfers, slow down"),
        },
        tags=["Transfers"],
    )
    def post(self, request):
        key = get_idempotency_key(request)
        serializer = TransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.transfer(request.user, data["to_email"], data["amount"], key, data["note"])
        return to_response(result)


@extend_schema(summary="Your transaction history (newest first)", tags=["Transfers"])
class TransactionListView(generics.ListAPIView):
    serializer_class = LedgerEntrySerializer

    def get_queryset(self):
        return (
            LedgerEntry.objects.filter(wallet__user=self.request.user)
            .select_related("counterparty__user__merchant", "hold")
            .order_by("-created_at", "-id")
        )


# ------------------------------------------------------------------ payment requests
class PaymentRequestListCreateView(generics.ListAPIView):
    serializer_class = PaymentRequestSerializer
    throttle_scope = "requests"

    def get_throttles(self):
        # Only rate-limit creating requests, not listing them.
        return super().get_throttles() if self.request.method == "POST" else []

    @extend_schema(
        summary="Your payment requests",
        parameters=[
            OpenApiParameter("direction", str, enum=["incoming", "outgoing"],
                             description="incoming = others asking you; outgoing = you asking others"),
            OpenApiParameter("status", str, enum=[s for s, _ in PaymentRequest.Status.choices]),
        ],
        tags=["Requests"],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        user = self.request.user
        direction = self.request.query_params.get("direction")
        if direction == "incoming":
            qs = PaymentRequest.objects.filter(payer=user)
        elif direction == "outgoing":
            qs = PaymentRequest.objects.filter(requester=user)
        else:
            qs = PaymentRequest.objects.filter(payer=user) | PaymentRequest.objects.filter(requester=user)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        return qs.select_related("requester", "payer").order_by("-created_at", "-id")

    @extend_schema(
        summary="Ask someone for money",
        request=PaymentRequestCreateSerializer,
        responses={201: PaymentRequestSerializer},
        tags=["Requests"],
    )
    def post(self, request):
        serializer = PaymentRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        payment_request = services.create_request(
            request.user, data["from_email"], data["amount"], data["note"]
        )
        return Response(
            PaymentRequestSerializer(payment_request, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class PayRequestView(APIView):
    throttle_scope = "transfers"  # paying moves money, so it shares the transfer limit

    @extend_schema(
        summary="Pay a request someone sent you",
        request=None,
        parameters=[IDEMPOTENCY_PARAM],
        responses={
            200: OpenApiResponse(description="Paid"),
            202: OpenApiResponse(description="On hold for a security review (risk rules)"),
            400: OpenApiResponse(description="Insufficient balance"),
            404: OpenApiResponse(description="Not found, or not addressed to you"),
            409: OpenApiResponse(description="Already paid, declined or cancelled"),
        },
        tags=["Requests"],
    )
    def post(self, request, pk):
        key = get_idempotency_key(request)
        return to_response(services.pay_request(request.user, pk, key))


class DeclineRequestView(APIView):
    @extend_schema(summary="Decline a request someone sent you", request=None,
                   responses=PaymentRequestSerializer, tags=["Requests"])
    def post(self, request, pk):
        payment_request = services.decline_request(request.user, pk)
        return Response(PaymentRequestSerializer(payment_request, context={"request": request}).data)


class CancelRequestView(APIView):
    @extend_schema(summary="Cancel a request you sent", request=None,
                   responses=PaymentRequestSerializer, tags=["Requests"])
    def post(self, request, pk):
        payment_request = services.cancel_request(request.user, pk)
        return Response(PaymentRequestSerializer(payment_request, context={"request": request}).data)
