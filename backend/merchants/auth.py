"""
Merchant API keys: "Authorization: Bearer sk_test_..." (like Stripe).

The key is hashed with SHA-256 and looked up by hash, so the database never
holds a usable key. On success request.user is the merchant's owner and
request.auth is the Merchant.
"""
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from .models import Merchant, hash_api_key


class MerchantKeyAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode()
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword or not parts[1].startswith("sk_"):
            return None
        merchant = Merchant.objects.select_related("owner").filter(api_key_hash=hash_api_key(parts[1])).first()
        if merchant is None or not merchant.owner.is_active:
            raise exceptions.AuthenticationFailed("Invalid API key.")
        return merchant.owner, merchant

    def authenticate_header(self, request):
        return self.keyword


class MerchantKeyScheme(OpenApiAuthenticationExtension):
    target_class = "merchants.auth.MerchantKeyAuthentication"
    name = "MerchantApiKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "Your secret API key (sk_test_...), from the Business page.",
        }
