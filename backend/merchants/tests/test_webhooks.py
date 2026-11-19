import json
import time
import urllib.error
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from demoshop.models import ShopOrder
from demoshop.views import MERCHANT_SLUG
from merchants import services, webhooks
from merchants.models import Merchant, WebhookEvent
from wallets.tests.helpers import WalletTestMixin, make_old, new_key


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(status):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, None)
        return FakeResponse(status)

    opener.calls = calls
    return opener


def opener_into(client):
    """Deliver webhooks into this Django test client, like a real HTTP POST would."""

    def opener(request, timeout):
        res = client.post(
            "/shop/webhooks/pockit", data=request.data, content_type="application/json",
            **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in request.headers.items()},
        )
        if res.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, res.status_code, "error", {}, None)
        return FakeResponse(res.status_code)

    return opener


class SignatureTests(TestCase):
    def test_round_trip(self):
        body = b'{"id":"1"}'
        self.assertTrue(webhooks.verify("whsec_x", body, webhooks.sign("whsec_x", body)))

    def test_tampered_body_or_wrong_secret_fails(self):
        header = webhooks.sign("whsec_x", b'{"amount":"10"}')
        self.assertFalse(webhooks.verify("whsec_x", b'{"amount":"99"}', header))
        self.assertFalse(webhooks.verify("whsec_other", b'{"amount":"10"}', header))

    def test_old_signature_is_rejected(self):
        body = b"{}"
        header = webhooks.sign("whsec_x", body, timestamp=int(time.time()) - 3600)
        self.assertFalse(webhooks.verify("whsec_x", body, header))

    def test_garbage_header(self):
        self.assertFalse(webhooks.verify("whsec_x", b"{}", "nonsense"))


class DeliveryTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        owner = self.signup("Meera", "meera@example.com")
        owner.post("/api/merchant", {"name": "Shop"}, format="json")
        # Set directly: the API's SSRF guard would (rightly) refuse an unresolvable test host.
        Merchant.objects.update(webhook_url="https://shop.example/hook")
        self.merchant = Merchant.objects.get()
        self.event = webhooks.enqueue(self.merchant, "order.paid", {"order_id": "x"})

    def test_success(self):
        opener = opener_returning(200)
        webhooks.deliver(self.event.id, opener=opener)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "DELIVERED")
        sent = opener.calls[0]
        self.assertTrue(webhooks.verify(self.merchant.webhook_secret, sent.data, sent.headers["Pockit-signature"]))

    def test_failure_schedules_a_retry_with_backoff(self):
        webhooks.deliver(self.event.id, opener=opener_returning(500))
        self.event.refresh_from_db()
        self.assertEqual((self.event.status, self.event.attempts, self.event.last_error), ("PENDING", 1, "HTTP 500"))
        self.assertGreater(self.event.next_attempt_at, timezone.now() + timedelta(seconds=20))
        # not due yet, so the retry worker leaves it alone
        self.assertEqual(webhooks.deliver_due(opener=opener_returning(200)), 0)

    def test_gives_up_after_max_attempts(self):
        for _ in range(6):
            WebhookEvent.objects.filter(id=self.event.id).update(next_attempt_at=timezone.now())
            webhooks.deliver_due(opener=opener_returning(503))
        self.event.refresh_from_db()
        self.assertEqual((self.event.status, self.event.attempts), ("FAILED", 6))

    def test_delivered_events_are_never_sent_again(self):
        webhooks.deliver(self.event.id, opener=opener_returning(200))
        opener = opener_returning(200)
        self.assertIsNone(webhooks.deliver(self.event.id, opener=opener))
        self.assertEqual(opener.calls, [])


class DemoShopEndToEndTests(WalletTestMixin, TestCase):
    """Shop → checkout → webhook → shop marks it paid → refund → webhook again."""

    def setUp(self):
        super().setUp()
        owner = self.signup("Chai and Co owner", "chai@example.com")
        self.owner_user = get_user_model().objects.get(email="chai@example.com")
        self.merchant, _ = services.create_merchant(self.owner_user, "Chai & Co.", "http://testserver/shop/webhooks/pockit")
        Merchant.objects.filter(id=self.merchant.id).update(slug=MERCHANT_SLUG)
        self.merchant.refresh_from_db()
        self.owner = owner
        self.customer = self.signup("Aarav", "aarav@example.com")
        make_old("aarav@example.com")
        self.topup(self.customer, 2000)
        self.browser = Client()

    def buy(self, product="gift-box"):
        res = self.browser.post("/shop/api/buy", {"product": product}, content_type="application/json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()

    def test_full_flow(self):
        bought = self.buy()
        order_id = bought["checkout_url"].split("order=")[1]
        self.customer.post(f"/api/checkout/{order_id}/pay", HTTP_IDEMPOTENCY_KEY=new_key())

        # Before the webhook arrives the shop still waits: it doesn't trust the redirect.
        self.assertEqual(ShopOrder.objects.get(id=bought["shop_order_id"]).status, "WAITING")

        webhooks.deliver_due(opener=opener_into(self.browser))
        shop_order = self.browser.get(f"/shop/api/orders/{bought['shop_order_id']}").json()
        self.assertEqual(shop_order["status"], "PAID")

        self.owner.post(f"/api/merchant/orders/{order_id}/refunds", {"amount": "250"}, format="json",
                        HTTP_IDEMPOTENCY_KEY=new_key())
        webhooks.deliver_due(opener=opener_into(self.browser))
        shop_order = self.browser.get(f"/shop/api/orders/{bought['shop_order_id']}").json()
        self.assertEqual((shop_order["status"], shop_order["refunded_amount"]), ("PARTIALLY_REFUNDED", "250.00"))
        self.assertFalse(WebhookEvent.objects.exclude(status="DELIVERED").exists())

    def test_bad_signature_is_rejected(self):
        res = self.browser.post("/shop/webhooks/pockit", data=b"{}", content_type="application/json",
                                HTTP_POCKIT_SIGNATURE="t=1,v1=deadbeef")
        self.assertEqual(res.status_code, 400)

    def _signed_post(self, payload):
        body = json.dumps(payload).encode()
        return self.browser.post("/shop/webhooks/pockit", data=body, content_type="application/json",
                                 HTTP_POCKIT_SIGNATURE=webhooks.sign(self.merchant.webhook_secret, body))

    def test_duplicate_and_late_webhooks_are_harmless(self):
        bought = self.buy()
        order_id = bought["checkout_url"].split("order=")[1]
        refunded = {"id": "11111111-1111-1111-1111-111111111111", "type": "refund.created",
                    "data": {"order_id": order_id, "status": "REFUNDED", "refunded_amount": "1250.00"}}
        late_paid = {"id": "22222222-2222-2222-2222-222222222222", "type": "order.paid",
                     "data": {"order_id": order_id, "status": "PAID", "refunded_amount": "0.00"}}
        self.assertEqual(self._signed_post(refunded).status_code, 200)
        self.assertTrue(self._signed_post(refunded).json()["duplicate"])
        self._signed_post(late_paid)  # arrives late: must not move the order backwards
        self.assertEqual(ShopOrder.objects.get(id=bought["shop_order_id"]).status, "REFUNDED")

    def test_unknown_product(self):
        res = self.browser.post("/shop/api/buy", {"product": "pizza"}, content_type="application/json")
        self.assertEqual(res.status_code, 400)


class WebhookUrlValidationTests(WalletTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.signup("Meera", "meera@example.com")

    def test_private_addresses_are_refused_in_production(self):
        for url in ("http://127.0.0.1:8000/hook", "http://localhost/hook", "http://10.0.0.5/hook", "ftp://x.com/a"):
            res = self.owner.post("/api/merchant", {"name": "Shop", "webhook_url": url}, format="json")
            self.assertEqual(res.status_code, 400, url)

    @override_settings(DEBUG=True)
    def test_localhost_is_allowed_while_developing(self):
        res = self.owner.post("/api/merchant", {"name": "Shop", "webhook_url": "http://127.0.0.1:8000/hook"}, format="json")
        self.assertEqual(res.status_code, 201)
