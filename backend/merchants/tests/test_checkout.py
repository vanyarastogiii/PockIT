from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from merchants.models import Order, WebhookEvent
from wallets import services as wallet_services
from wallets.models import PaymentHold
from wallets.tests.helpers import WalletTestMixin, make_old, new_key


class MerchantTestMixin(WalletTestMixin):
    def setUp(self):
        super().setUp()
        self.shop_owner = self.signup("Meera", "meera@example.com")
        res = self.shop_owner.post("/api/merchant", {"name": "Chai Shop"}, format="json")
        assert res.status_code == 201, res.data
        self.api_key = res.data["api_key"]
        self.server = APIClient()  # the shop's backend
        self.server.credentials(HTTP_AUTHORIZATION=f"Bearer {self.api_key}")

        self.customer = self.signup("Aarav", "aarav@example.com")
        make_old("aarav@example.com")
        self.topup(self.customer, 5000)

    def create_order(self, amount="250.00", **extra):
        res = self.server.post("/api/v1/orders", {"amount": amount, "description": "Gift box", **extra}, format="json")
        assert res.status_code == 201, res.data
        return res.data

    def pay(self, client, order_id, key=None):
        return client.post(f"/api/checkout/{order_id}/pay", HTTP_IDEMPOTENCY_KEY=key or new_key())

    def refund(self, order_id, amount, key=None, reason=""):
        return self.shop_owner.post(
            f"/api/merchant/orders/{order_id}/refunds", {"amount": str(amount), "reason": reason},
            format="json", HTTP_IDEMPOTENCY_KEY=key or new_key(),
        )


class MerchantAccountTests(MerchantTestMixin, TestCase):
    def test_api_key_is_shown_once_and_stored_hashed(self):
        self.assertTrue(self.api_key.startswith("sk_test_"))
        profile = self.shop_owner.get("/api/merchant").data
        self.assertNotIn("api_key", profile)
        self.assertEqual(profile["api_key_hint"], self.api_key[:12] + "…")

    def test_cannot_become_merchant_twice(self):
        self.assertEqual(self.shop_owner.post("/api/merchant", {"name": "Again"}, format="json").status_code, 409)

    def test_non_merchant_gets_404(self):
        self.assertEqual(self.customer.get("/api/merchant").status_code, 404)

    def test_rotating_the_key_disables_the_old_one(self):
        new_key_value = self.shop_owner.post("/api/merchant/rotate-key").data["api_key"]
        self.assertEqual(self.server.post("/api/v1/orders", {"amount": "10", "description": "x"}, format="json").status_code, 401)
        self.server.credentials(HTTP_AUTHORIZATION=f"Bearer {new_key_value}")
        self.assertEqual(self.server.post("/api/v1/orders", {"amount": "10", "description": "x"}, format="json").status_code, 201)


class MerchantApiAuthTests(MerchantTestMixin, TestCase):
    def test_wrong_or_missing_key_is_rejected(self):
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION="Bearer sk_test_not_a_real_key")
        self.assertEqual(bad.post("/api/v1/orders", {"amount": "10", "description": "x"}).status_code, 401)
        self.assertEqual(APIClient().post("/api/v1/orders", {"amount": "10", "description": "x"}).status_code, 401)

    def test_a_user_login_token_is_not_a_merchant_key(self):
        self.assertEqual(self.shop_owner.post("/api/v1/orders", {"amount": "10", "description": "x"}).status_code, 401)

    def test_merchant_only_sees_own_orders(self):
        order = self.create_order()
        other = self.signup("Other", "other@example.com")
        other_key = other.post("/api/merchant", {"name": "Other Shop"}, format="json").data["api_key"]
        other_server = APIClient()
        other_server.credentials(HTTP_AUTHORIZATION=f"Bearer {other_key}")
        self.assertEqual(other_server.get(f"/api/v1/orders/{order['id']}").status_code, 404)


class CheckoutTests(MerchantTestMixin, TestCase):
    def test_order_has_a_checkout_link(self):
        order = self.create_order()
        self.assertIn(f"/checkout.html?order={order['id']}", order["checkout_url"])
        self.assertEqual(order["status"], "CREATED")

    def test_checkout_page_info_is_public_but_has_no_customer_data(self):
        order = self.create_order()
        res = APIClient().get(f"/api/checkout/{order['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["merchant_name"], "Chai Shop")
        self.assertNotIn("customer_email", res.data)

    def test_paying_moves_money_and_tells_the_merchant(self):
        order = self.create_order("250.00")
        res = self.pay(self.customer, order["id"])
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "PAID")
        self.assertEqual(self.balance(self.customer), Decimal("4750"))
        self.assertEqual(self.balance(self.shop_owner), Decimal("250"))
        # passbooks
        self.assertEqual(self.customer.get("/api/transactions").data["results"][0]["description"], "Paid ₹250.00 to Chai Shop")
        self.assertEqual(self.shop_owner.get("/api/transactions").data["results"][0]["description"], "Payment of ₹250.00 from Aarav")
        # outbox: the webhook was written in the same transaction
        event = WebhookEvent.objects.get()
        self.assertEqual(event.event_type, "order.paid")
        self.assertEqual(event.payload["data"]["customer_email"], "aarav@example.com")
        self.assert_ledger_consistent()

    def test_double_click_pays_once(self):
        order = self.create_order()
        key = new_key()
        first, second = self.pay(self.customer, order["id"], key), self.pay(self.customer, order["id"], key)
        self.assertEqual(first.data, second.data)
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(self.balance(self.customer), Decimal("4750"))

    def test_paying_from_two_tabs_is_rejected(self):
        order = self.create_order()
        self.assertEqual(self.pay(self.customer, order["id"]).status_code, 200)
        self.assertEqual(self.pay(self.customer, order["id"]).status_code, 409)
        self.assertEqual(self.balance(self.customer), Decimal("4750"))

    def test_cannot_pay_own_shop(self):
        self.topup(self.shop_owner, 1000)
        self.assertEqual(self.pay(self.shop_owner, self.create_order()["id"]).status_code, 400)

    def test_insufficient_balance_leaves_order_payable(self):
        order = self.create_order("9000.00")
        self.assertEqual(self.pay(self.customer, order["id"]).status_code, 400)
        self.assertEqual(Order.objects.get(id=order["id"]).status, "CREATED")

    def test_expired_checkout_cannot_be_paid(self):
        order = self.create_order()
        Order.objects.filter(id=order["id"]).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.pay(self.customer, order["id"]).status_code, 409)
        self.assertEqual(APIClient().get(f"/api/checkout/{order['id']}").data["status"], "EXPIRED")

    def test_paying_requires_login(self):
        self.assertEqual(self.pay(APIClient(), self.create_order()["id"]).status_code, 401)


class RefundTests(MerchantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.order = self.create_order("1000.00")
        self.pay(self.customer, self.order["id"])

    def test_partial_then_full_refund(self):
        res = self.refund(self.order["id"], "300", reason="One cup broke")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["order_status"], "PARTIALLY_REFUNDED")
        self.assertEqual(self.balance(self.customer), Decimal("4300"))
        self.assertEqual(self.customer.get("/api/transactions").data["results"][0]["description"], "Refund of ₹300.00 from Chai Shop")

        res = self.refund(self.order["id"], "700")
        self.assertEqual(res.data["order_status"], "REFUNDED")
        self.assertEqual(self.balance(self.customer), Decimal("5000"))
        self.assertEqual(self.balance(self.shop_owner), Decimal("0"))
        self.assertEqual(WebhookEvent.objects.filter(event_type="refund.created").count(), 2)
        self.assert_ledger_consistent()

    def test_cannot_refund_more_than_paid(self):
        self.refund(self.order["id"], "600")
        res = self.refund(self.order["id"], "500")
        self.assertEqual(res.status_code, 400)
        self.assertIn("400.00", str(res.data["detail"]))

    def test_cannot_refund_a_fully_refunded_order(self):
        self.refund(self.order["id"], "1000")
        self.assertEqual(self.refund(self.order["id"], "1").status_code, 409)

    def test_refund_retry_is_not_paid_twice(self):
        key = new_key()
        self.refund(self.order["id"], "200", key=key)
        again = self.refund(self.order["id"], "200", key=key)
        self.assertEqual(again["Idempotent-Replayed"], "true")
        self.assertEqual(self.balance(self.customer), Decimal("4200"))

    def test_refund_needs_merchant_balance(self):
        friend = self.signup("Friend", "friend@example.com")
        self.send(self.shop_owner, "friend@example.com", 900)  # the shop spent most of it
        res = self.refund(self.order["id"], "500")
        self.assertEqual(res.status_code, 400)
        self.assertIn("too low", str(res.data["detail"]))

    def test_refund_through_merchant_api_with_secret_key(self):
        res = self.server.post(
            f"/api/v1/orders/{self.order['id']}/refunds", {"amount": "100"}, format="json",
            HTTP_IDEMPOTENCY_KEY=new_key(),
        )
        self.assertEqual(res.status_code, 201)

    def test_other_users_cannot_refund(self):
        res = self.customer.post(
            f"/api/merchant/orders/{self.order['id']}/refunds", {"amount": "100"}, format="json",
            HTTP_IDEMPOTENCY_KEY=new_key(),
        )
        self.assertEqual(res.status_code, 404)

    def test_business_page_lists_paid_orders(self):
        self.create_order()  # unpaid: not listed
        orders = self.shop_owner.get("/api/merchant/orders").data["results"]
        self.assertEqual([o["id"] for o in orders], [self.order["id"]])
        self.assertEqual(orders[0]["customer_name"], "Aarav")
        stats = self.shop_owner.get("/api/merchant").data["stats"]
        self.assertEqual(stats["total_received"], "1000.00")


class HeldCheckoutTests(MerchantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.newbie = self.signup("Newbie", "newbie@example.com")  # account just created
        self.topup(self.newbie, 20000)
        self.order = self.create_order("8000.00")
        res = self.pay(self.newbie, self.order["id"])
        self.assertEqual(res.status_code, 202, res.data)
        self.hold = PaymentHold.objects.get()

    def test_order_is_on_hold_and_merchant_is_told(self):
        self.assertEqual(Order.objects.get(id=self.order["id"]).status, "ON_HOLD")
        self.assertEqual(self.balance(self.shop_owner), Decimal("0"))
        self.assertEqual(list(WebhookEvent.objects.values_list("event_type", flat=True)), ["order.on_hold"])

    def test_cannot_refund_while_on_hold(self):
        self.assertEqual(self.refund(self.order["id"], "100").status_code, 409)

    def test_approve_completes_the_order(self):
        wallet_services.release_hold(self.hold.id)
        self.assertEqual(Order.objects.get(id=self.order["id"]).status, "PAID")
        self.assertEqual(self.balance(self.shop_owner), Decimal("8000"))
        self.assertTrue(WebhookEvent.objects.filter(event_type="order.paid").exists())
        self.assert_ledger_consistent()

    def test_reject_declines_the_order_and_returns_money(self):
        wallet_services.reject_hold(self.hold.id)
        self.assertEqual(Order.objects.get(id=self.order["id"]).status, "DECLINED")
        self.assertEqual(self.balance(self.newbie), Decimal("20000"))
        self.assertTrue(WebhookEvent.objects.filter(event_type="order.declined").exists())
        self.assert_ledger_consistent()
