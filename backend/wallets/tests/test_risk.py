from decimal import Decimal

from django.test import TestCase, override_settings

from wallets import services
from wallets.models import LedgerEntry, PaymentHold, PaymentRequest, Wallet

from .helpers import WalletTestMixin, make_old, new_key


def holds_balance():
    return Wallet.objects.get(kind=Wallet.Kind.SYSTEM, name=services.HOLDS_WALLET_NAME).balance


class RiskRuleTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.rahul = self.signup("Rahul", "rahul@example.com")
        self.priya = self.signup("Priya", "priya@example.com")
        self.topup(self.rahul, 100000)

    # ---------------------------------------------------------- rule: new account, large amount
    def test_new_account_large_payment_is_held(self):
        res = self.send(self.rahul, "priya@example.com", 6000)
        self.assertEqual(res.status_code, 202, res.data)
        self.assertEqual(res.data["status"], "ON_HOLD")
        self.assertEqual(res.data["reasons"][0]["code"], "new_account_large")
        # money has left Rahul but hasn't reached Priya; it sits in the holding wallet
        self.assertEqual(self.balance(self.rahul), Decimal("94000"))
        self.assertEqual(self.balance(self.priya), Decimal("0"))
        self.assertEqual(holds_balance(), Decimal("6000"))
        self.assert_ledger_consistent()

    def test_new_account_small_payment_goes_through(self):
        self.assertEqual(self.send(self.rahul, "priya@example.com", 5000).status_code, 201)

    def test_old_account_is_not_held_for_same_amount(self):
        make_old("rahul@example.com")
        self.assertEqual(self.send(self.rahul, "priya@example.com", 6000).status_code, 201)

    # ---------------------------------------------------------- rule: velocity
    def test_sixth_payment_in_ten_minutes_is_held(self):
        codes = [self.send(self.rahul, "priya@example.com", 10).status_code for _ in range(6)]
        self.assertEqual(codes, [201, 201, 201, 201, 201, 202])

    # ---------------------------------------------------------- rule: new recipient, large
    def test_large_first_payment_to_new_person_is_held(self):
        make_old("rahul@example.com")
        res = self.send(self.rahul, "priya@example.com", 12000)
        self.assertEqual(res.status_code, 202)
        self.assertEqual([r["code"] for r in res.data["reasons"]], ["new_recipient_large"])

    def test_large_payment_to_someone_paid_before_goes_through(self):
        make_old("rahul@example.com")
        self.send(self.rahul, "priya@example.com", 100)
        self.assertEqual(self.send(self.rahul, "priya@example.com", 12000).status_code, 201)

    # ---------------------------------------------------------- rule: very large
    def test_very_large_payment_is_always_held(self):
        make_old("rahul@example.com")
        self.send(self.rahul, "priya@example.com", 100)
        res = self.send(self.rahul, "priya@example.com", 50000)
        self.assertEqual(res.status_code, 202)
        self.assertEqual([r["code"] for r in res.data["reasons"]], ["large_amount"])

    @override_settings(RISK_RULES={"ENABLED": False})
    def test_rules_can_be_switched_off(self):
        self.assertEqual(self.send(self.rahul, "priya@example.com", 60000).status_code, 201)

    # ---------------------------------------------------------- holds: idempotency
    def test_retrying_a_held_payment_does_not_hold_twice(self):
        key = new_key()
        first = self.send(self.rahul, "priya@example.com", 6000, key=key)
        second = self.send(self.rahul, "priya@example.com", 6000, key=key)
        self.assertEqual(first.data, second.data)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(PaymentHold.objects.count(), 1)
        self.assertEqual(self.balance(self.rahul), Decimal("94000"))

    def test_insufficient_balance_is_checked_before_holding(self):
        res = self.send(self.priya, "rahul@example.com", 6000)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(PaymentHold.objects.count(), 0)


class HoldReviewTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.rahul = self.signup("Rahul", "rahul@example.com")
        self.priya = self.signup("Priya", "priya@example.com")
        self.topup(self.rahul, 10000)
        self.hold_id = self.send(self.rahul, "priya@example.com", 6000, note="Rent").data["hold_id"]

    def sender_row(self):
        return self.rahul.get("/api/transactions").data["results"][0]

    def test_passbook_shows_payment_on_hold(self):
        row = self.sender_row()
        self.assertEqual(row["description"], "Sent ₹6,000.00 to Priya")
        self.assertEqual(row["hold_status"], "HELD")
        self.assertEqual(self.priya.get("/api/transactions").data["count"], 0)

    def test_approve_sends_money_on(self):
        services.release_hold(self.hold_id)
        self.assertEqual(self.balance(self.priya), Decimal("6000"))
        self.assertEqual(holds_balance(), Decimal("0"))
        self.assertEqual(self.sender_row()["hold_status"], "RELEASED")
        received = self.priya.get("/api/transactions").data["results"][0]
        self.assertEqual(received["description"], "Received ₹6,000.00 from Rahul")
        self.assertEqual(received["note"], "Rent")
        self.assert_ledger_consistent()

    def test_reject_returns_money(self):
        services.reject_hold(self.hold_id)
        self.assertEqual(self.balance(self.rahul), Decimal("10000"))
        self.assertEqual(self.balance(self.priya), Decimal("0"))
        latest = self.sender_row()
        self.assertEqual(latest["entry_type"], LedgerEntry.EntryType.HOLD_RETURN)
        self.assertEqual(latest["description"], "Returned ₹6,000.00: payment to Priya wasn't approved")
        self.assert_ledger_consistent()

    def test_review_is_safe_to_repeat_but_not_to_reverse(self):
        services.release_hold(self.hold_id)
        services.release_hold(self.hold_id)  # no-op
        self.assertEqual(self.balance(self.priya), Decimal("6000"))
        with self.assertRaises(services.HoldNotHeld):
            services.reject_hold(self.hold_id)


class HeldRequestPaymentTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.rahul = self.signup("Rahul", "rahul@example.com")
        self.priya = self.signup("Priya", "priya@example.com")
        self.topup(self.rahul, 10000)
        self.req_id = self.priya.post(
            "/api/requests", {"from_email": "rahul@example.com", "amount": "7000"}, format="json"
        ).data["id"]
        res = self.rahul.post(f"/api/requests/{self.req_id}/pay", HTTP_IDEMPOTENCY_KEY=new_key())
        self.assertEqual(res.status_code, 202)
        self.hold = PaymentHold.objects.get()

    def test_request_is_on_hold_and_cannot_be_paid_again(self):
        self.assertEqual(PaymentRequest.objects.get(id=self.req_id).status, "ON_HOLD")
        again = self.rahul.post(f"/api/requests/{self.req_id}/pay", HTTP_IDEMPOTENCY_KEY=new_key())
        self.assertEqual(again.status_code, 409)

    def test_approve_marks_request_paid(self):
        services.release_hold(self.hold.id)
        self.assertEqual(PaymentRequest.objects.get(id=self.req_id).status, "PAID")
        self.assertEqual(self.balance(self.priya), Decimal("7000"))

    def test_reject_puts_request_back_to_pending(self):
        services.reject_hold(self.hold.id)
        self.assertEqual(PaymentRequest.objects.get(id=self.req_id).status, "PENDING")
        self.assertEqual(self.balance(self.rahul), Decimal("10000"))
        self.assert_ledger_consistent()
