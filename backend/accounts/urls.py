from django.urls import path
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework_simplejwt.views import TokenRefreshView

from .views import LoginView, MeView, SignupView

RefreshView = extend_schema_view(
    post=extend_schema(summary="Get a new access token using your refresh token", tags=["Auth"], auth=[])
)(TokenRefreshView)

urlpatterns = [
    path("signup", SignupView.as_view(), name="signup"),
    path("login", LoginView.as_view(), name="login"),
    path("refresh", RefreshView.as_view(), name="token-refresh"),
    path("me", MeView.as_view(), name="me"),
]
