import uuid
from decimal import Decimal

from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APIClient


def new_key():
    return str(uuid.uuid4())


class WalletTestMixin:
    """Shared helpers for API tests."""

    def setUp(self):
        super().setUp()
        cache.clear()  # reset rate-limit counters between tests

    def signup(self, name, email, password="s3cure-pass-123"):
        client = APIClient()
        res = client.post(
            "/api/auth/signup",
            {"name": name, "email": email, "password": password},
            format="json",
        )
        assert res.status_code == 201, res.data
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        return client

    def topup(self, client, amount, key=None):
        return client.post(
            "/api/wallet/topup",
            {"amount": str(amount)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or new_key(),
        )

    def send(self, client, to_email, amount, key=None, note=""):
        return client.post(
            "/api/transfers",
            {"to_email": to_email, "amount": str(amount), "note": note},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or new_key(),
        )

    def balance(self, client):
        return Decimal(client.get("/api/wallet").data["balance"])

    def assert_ledger_consistent(self):
        # Raises CommandError if any wallet disagrees with its ledger.
        call_command("reconcile", stdout=open("/dev/null", "w"))


def make_old(email, days=30):
    """Pretend an account was created long ago (so the new-account risk rule doesn't fire)."""
    from datetime import timedelta

    from django.contrib.auth import get_user_model
    from django.utils import timezone

    get_user_model().objects.filter(email=email).update(date_joined=timezone.now() - timedelta(days=days))
