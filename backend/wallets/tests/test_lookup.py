from unittest.mock import patch

from django.test import TestCase
from rest_framework.throttling import ScopedRateThrottle

from .helpers import WalletTestMixin


class RecipientLookupTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.rahul = self.signup("Rahul Verma", "rahul@example.com")
        self.signup("Priya Sharma", "priya@example.com")

    def lookup(self, email):
        return self.rahul.get("/api/users/lookup", {"email": email})

    def test_existing_user_returns_name(self):
        res = self.lookup("priya@example.com")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"name": "Priya Sharma", "email": "priya@example.com"})

    def test_lookup_ignores_case_and_spaces(self):
        self.assertEqual(self.lookup("  PRIYA@Example.com ").status_code, 200)

    def test_unknown_email(self):
        res = self.lookup("nobody@example.com")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "not_found")

    def test_own_email(self):
        res = self.lookup("rahul@example.com")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "self")

    def test_invalid_email(self):
        self.assertEqual(self.lookup("not-an-email").status_code, 400)
        self.assertEqual(self.lookup("").status_code, 400)

    def test_no_partial_matches(self):
        """Exact email only, so nobody can search the user list."""
        self.assertEqual(self.lookup("priya@").status_code, 400)
        self.assertEqual(self.lookup("priya@example.co").status_code, 404)

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/users/lookup", {"email": "priya@example.com"}).status_code, 401)

    @patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"lookups": "3/min"})
    def test_rate_limited(self):
        codes = [self.lookup("priya@example.com").status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
