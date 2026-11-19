from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from wallets.models import Wallet

from .serializers import LoginSerializer, SignupSerializer, UserSerializer

User = get_user_model()


def tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class SignupView(APIView):
    """POST /api/auth/signup  -> creates a user AND their ₹0 wallet."""

    permission_classes = [AllowAny]

    @extend_schema(summary="Create an account and a ₹0 wallet", request=SignupSerializer, auth=[],
                   responses={201: OpenApiResponse(description="User, wallet and JWT tokens")}, tags=["Auth"])
    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # User + wallet are created together: never a user without a wallet.
        with transaction.atomic():
            user = User.objects.create_user(
                username=data["email"],
                email=data["email"],
                password=data["password"],
                first_name=data["name"],
            )
            Wallet.objects.create(user=user)

        return Response(
            {"user": UserSerializer(user).data, **tokens_for(user)},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """POST /api/auth/login  -> returns JWT access + refresh tokens."""

    permission_classes = [AllowAny]

    @extend_schema(summary="Log in and get a JWT access token", request=LoginSerializer, auth=[],
                   responses={200: OpenApiResponse(description="User and JWT tokens")}, tags=["Auth"])
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        return Response({"user": UserSerializer(user).data, **tokens_for(user)})


class MeView(APIView):
    """GET /api/auth/me  -> the logged-in user."""

    @extend_schema(summary="The logged-in user", responses=UserSerializer, tags=["Auth"])
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class RecipientLookupView(APIView):
    """
    GET /api/users/lookup?email=priya@example.com

    Confirms who you're about to pay before any money moves, like UPI showing
    the payee's name. Privacy trade-off: this reveals whether an email has an
    account, so it is only available to logged-in users, matches the exact
    email only (no partial search), and is rate-limited to stop scraping.
    """

    throttle_scope = "lookups"

    @extend_schema(
        summary="Check that an email belongs to a PockIt user",
        parameters=[OpenApiParameter("email", str, required=True, description="Exact email address")],
        responses={
            200: OpenApiResponse(description="Account found: returns the name"),
            400: OpenApiResponse(description="Missing/invalid email, or your own email"),
            404: OpenApiResponse(description="No account with this email"),
            429: OpenApiResponse(description="Too many lookups, slow down"),
        },
        tags=["Auth"],
    )
    def get(self, request):
        email = request.query_params.get("email", "").strip().lower()
        try:
            validate_email(email)
        except DjangoValidationError:
            return Response({"detail": "Enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)

        if email == request.user.email.lower():
            return Response(
                {"detail": "That's your own email.", "code": "self"}, status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.filter(email__iexact=email).only("first_name", "email").first()
        if user is None:
            return Response(
                {"detail": "No PockIt account uses this email.", "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"name": user.first_name, "email": user.email})
