from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.throttling import ScopedRateThrottle

from wallets.models import LedgerEntry

from .helpers import WalletTestMixin, new_key


class AuthTests(WalletTestMixin, TestCase):
    def test_signup_creates_zero_wallet(self):
        client = self.signup("Rahul", "rahul@example.com")
        self.assertEqual(self.balance(client), Decimal("0"))

    def test_duplicate_email_rejected(self):
        self.signup("Rahul", "rahul@example.com")
        res = self.client.post(
            "/api/auth/signup",
            {"name": "R2", "email": "RAHUL@example.com", "password": "s3cure-pass-123"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_login(self):
        self.signup("Rahul", "rahul@example.com")
        res = self.client.post(
            "/api/auth/login",
            {"email": "rahul@example.com", "password": "s3cure-pass-123"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.json())

    def test_wallet_requires_login(self):
        self.assertEqual(self.client.get("/api/wallet").status_code, 401)


class TopUpTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.rahul = self.signup("Rahul", "rahul@example.com")

    def test_topup_adds_money_and_ledger_row(self):
        res = self.topup(self.rahul, 500)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.balance(self.rahul), Decimal("500"))
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assert_ledger_consistent()

    def test_negative_and_zero_amounts_rejected(self):
        self.assertEqual(self.topup(self.rahul, -50).status_code, 400)
        self.assertEqual(self.topup(self.rahul, 0).status_code, 400)
        self.assertEqual(self.balance(self.rahul), Decimal("0"))

    def test_idempotency_key_is_required(self):
        res = self.rahul.post("/api/wallet/topup", {"amount": "10"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_double_click_topup_charges_once(self):
        key = new_key()
        first = self.topup(self.rahul, 500, key=key)
        second = self.topup(self.rahul, 500, key=key)
        self.assertEqual(first.data, second.data)
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(self.balance(self.rahul), Decimal("500"))


class TransferTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.rahul = self.signup("Rahul", "rahul@example.com")
        self.priya = self.signup("Priya", "priya@example.com")
        self.topup(self.rahul, 1000)

    def test_transfer_moves_money(self):
        res = self.send(self.rahul, "priya@example.com", 200)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.balance(self.rahul), Decimal("800"))
        self.assertEqual(self.balance(self.priya), Decimal("200"))
        self.assert_ledger_consistent()

    def test_transfer_writes_two_ledger_rows_that_sum_to_zero(self):
        res = self.send(self.rahul, "priya@example.com", 200)
        rows = LedgerEntry.objects.filter(reference=res.data["reference"])
        self.assertEqual(rows.count(), 2)
        self.assertEqual(sum(r.amount for r in rows), Decimal("0"))

    def test_insufficient_balance(self):
        res = self.send(self.rahul, "priya@example.com", 5000)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["detail"].code, "insufficient_funds")
        self.assertEqual(self.balance(self.rahul), Decimal("1000"))
        self.assertEqual(self.balance(self.priya), Decimal("0"))

    def test_cannot_send_to_self(self):
        res = self.send(self.rahul, "Rahul@Example.com", 10)
        self.assertEqual(res.status_code, 400)

    def test_unknown_recipient(self):
        res = self.send(self.rahul, "nobody@example.com", 10)
        self.assertEqual(res.status_code, 404)

    def test_double_click_transfer_charges_once(self):
        key = new_key()
        first = self.send(self.rahul, "priya@example.com", 500, key=key)
        second = self.send(self.rahul, "priya@example.com", 500, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.data["reference"], second.data["reference"])
        self.assertEqual(self.balance(self.rahul), Decimal("500"))  # not 0!
        self.assertEqual(self.balance(self.priya), Decimal("500"))
        self.assert_ledger_consistent()

    def test_same_key_different_payload_is_rejected(self):
        key = new_key()
        self.send(self.rahul, "priya@example.com", 100, key=key)
        res = self.send(self.rahul, "priya@example.com", 999, key=key)
        self.assertEqual(res.status_code, 422)

    def test_history_shows_human_descriptions(self):
        self.send(self.rahul, "priya@example.com", 200)
        rahul_history = self.rahul.get("/api/transactions").data["results"]
        priya_history = self.priya.get("/api/transactions").data["results"]
        self.assertEqual(rahul_history[0]["description"], "Sent ₹200.00 to Priya")
        self.assertEqual(priya_history[0]["description"], "Received ₹200.00 from Rahul")

    def test_history_is_private(self):
        self.send(self.rahul, "priya@example.com", 200)
        priya_history = self.priya.get("/api/transactions").data
        self.assertEqual(priya_history["count"], 1)  # only her own entry


class RateLimitTests(WalletTestMixin, TestCase):
    @patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"transfers": "3/min"})
    def test_transfers_are_rate_limited(self):
        rahul = self.signup("Rahul", "rahul@example.com")
        self.signup("Priya", "priya@example.com")
        self.topup(rahul, 100)
        codes = [self.send(rahul, "priya@example.com", 1).status_code for _ in range(4)]
        self.assertEqual(codes, [201, 201, 201, 429])
