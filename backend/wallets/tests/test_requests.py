from decimal import Decimal

from django.test import TestCase

from wallets.models import PaymentRequest
from wallets.serializers import format_inr

from .helpers import WalletTestMixin, new_key


class PaymentRequestTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.priya = self.signup("Priya", "priya@example.com")
        self.rahul = self.signup("Rahul", "rahul@example.com")
        self.topup(self.rahul, 1000)

    # ---------------------------------------------------------- helpers
    def ask(self, client, from_email, amount, note=""):
        return client.post(
            "/api/requests", {"from_email": from_email, "amount": str(amount), "note": note}, format="json"
        )

    def pay(self, client, request_id, key=None):
        return client.post(f"/api/requests/{request_id}/pay", HTTP_IDEMPOTENCY_KEY=key or new_key())

    # ---------------------------------------------------------- create / list
    def test_create_request_does_not_move_money(self):
        res = self.ask(self.priya, "rahul@example.com", 200, "Dinner")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["status"], "PENDING")
        self.assertEqual(res.data["direction"], "outgoing")
        self.assertEqual(self.balance(self.rahul), Decimal("1000"))

    def test_payer_sees_it_as_incoming(self):
        self.ask(self.priya, "rahul@example.com", 200)
        incoming = self.rahul.get("/api/requests?direction=incoming&status=pending").data["results"]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["other_name"], "Priya")
        self.assertEqual(incoming[0]["direction"], "incoming")

    def test_cannot_request_from_self_or_unknown_user(self):
        self.assertEqual(self.ask(self.priya, "priya@example.com", 10).status_code, 400)
        self.assertEqual(self.ask(self.priya, "nobody@example.com", 10).status_code, 404)

    def test_invalid_amount(self):
        self.assertEqual(self.ask(self.priya, "rahul@example.com", 0).status_code, 400)

    # ---------------------------------------------------------- pay
    def test_pay_moves_money_and_marks_paid(self):
        req_id = self.ask(self.priya, "rahul@example.com", 200, "Dinner").data["id"]
        res = self.pay(self.rahul, req_id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.balance(self.rahul), Decimal("800"))
        self.assertEqual(self.balance(self.priya), Decimal("200"))
        req = PaymentRequest.objects.get(id=req_id)
        self.assertEqual(req.status, "PAID")
        self.assertIsNotNone(req.transfer_reference)
        # the note travels into both ledger rows
        history = self.priya.get("/api/transactions").data["results"]
        self.assertEqual(history[0]["note"], "Dinner")
        self.assert_ledger_consistent()

    def test_double_click_pay_charges_once(self):
        req_id = self.ask(self.priya, "rahul@example.com", 200).data["id"]
        key = new_key()
        first = self.pay(self.rahul, req_id, key)
        second = self.pay(self.rahul, req_id, key)
        self.assertEqual(first.data, second.data)
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(self.balance(self.rahul), Decimal("800"))

    def test_paying_twice_from_two_tabs_is_rejected(self):
        """Different idempotency keys (two browser tabs) still can't pay twice."""
        req_id = self.ask(self.priya, "rahul@example.com", 200).data["id"]
        self.assertEqual(self.pay(self.rahul, req_id).status_code, 200)
        self.assertEqual(self.pay(self.rahul, req_id).status_code, 409)
        self.assertEqual(self.balance(self.rahul), Decimal("800"))

    def test_only_the_payer_can_pay(self):
        req_id = self.ask(self.priya, "rahul@example.com", 200).data["id"]
        self.assertEqual(self.pay(self.priya, req_id).status_code, 404)

    def test_pay_with_insufficient_balance_keeps_request_pending(self):
        req_id = self.ask(self.priya, "rahul@example.com", 5000).data["id"]
        res = self.pay(self.rahul, req_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(PaymentRequest.objects.get(id=req_id).status, "PENDING")
        self.assertEqual(self.balance(self.rahul), Decimal("1000"))

    # ---------------------------------------------------------- decline / cancel
    def test_decline_then_pay_is_rejected(self):
        req_id = self.ask(self.priya, "rahul@example.com", 200).data["id"]
        res = self.rahul.post(f"/api/requests/{req_id}/decline")
        self.assertEqual(res.data["status"], "DECLINED")
        self.assertEqual(self.pay(self.rahul, req_id).status_code, 409)
        self.assertEqual(self.balance(self.rahul), Decimal("1000"))

    def test_requester_can_cancel_but_payer_cannot(self):
        req_id = self.ask(self.priya, "rahul@example.com", 200).data["id"]
        self.assertEqual(self.rahul.post(f"/api/requests/{req_id}/cancel").status_code, 404)
        res = self.priya.post(f"/api/requests/{req_id}/cancel")
        self.assertEqual(res.data["status"], "CANCELLED")

    def test_cancel_after_paid_is_rejected(self):
        req_id = self.ask(self.priya, "rahul@example.com", 200).data["id"]
        self.pay(self.rahul, req_id)
        self.assertEqual(self.priya.post(f"/api/requests/{req_id}/cancel").status_code, 409)

    def test_requests_are_private(self):
        self.signup("Anil", "anil@example.com")
        anil = self.signup("Anil2", "anil2@example.com")
        self.ask(self.priya, "rahul@example.com", 200)
        self.assertEqual(anil.get("/api/requests").data["count"], 0)


class FormattingTests(TestCase):
    def test_indian_grouping(self):
        self.assertEqual(format_inr(Decimal("200")), "₹200.00")
        self.assertEqual(format_inr(Decimal("2000")), "₹2,000.00")
        self.assertEqual(format_inr(Decimal("100000")), "₹1,00,000.00")
        self.assertEqual(format_inr(Decimal("1234567.5")), "₹12,34,567.50")
