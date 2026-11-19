"""
Real concurrency tests: several threads hit the service at the same moment.

These only mean something on PostgreSQL, because SQLite ignores
SELECT ... FOR UPDATE. Run them with:

    docker compose up -d db
    POSTGRES_DB=wallet python manage.py test wallets.tests.test_concurrency
"""
import threading
import unittest
from unittest.mock import patch
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase

from wallets import services
from wallets.models import Wallet

from .helpers import new_key

User = get_user_model()


def run_together(fns):
    """Start one thread per function; all call at the same instant. Returns outcomes in order."""
    barrier = threading.Barrier(len(fns))
    outcomes = [None] * len(fns)

    def worker(i, fn):
        try:
            barrier.wait()
            outcomes[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001
            outcomes[i] = ("error", exc)
        finally:
            connections.close_all()  # each thread has its own DB connection

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def run_at_once(fn, n):
    """Call the same function from n threads at the same instant."""
    return run_together([fn] * n)


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row locks")
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice@x.com", "alice@x.com", "pw", first_name="Alice")
        self.bob = User.objects.create_user("bob@x.com", "bob@x.com", "pw", first_name="Bob")
        Wallet.objects.create(user=self.alice)
        Wallet.objects.create(user=self.bob)
        services.top_up(self.alice, Decimal("100"), new_key())

    def test_two_simultaneous_80_transfers_from_100(self):
        """The example from the plan: only ONE can succeed, balance never goes negative."""
        outcomes = run_at_once(
            lambda: services.transfer(self.alice, "bob@x.com", Decimal("80"), new_key()), 2
        )
        successes = [o for o in outcomes if o[0] == "ok"]
        failures = [o for o in outcomes if o[0] == "error"]
        self.assertEqual(len(successes), 1)
        self.assertIsInstance(failures[0][1], services.InsufficientFunds)
        self.assertEqual(Wallet.objects.get(user=self.alice).balance, Decimal("20"))
        self.assertEqual(Wallet.objects.get(user=self.bob).balance, Decimal("80"))

    def test_same_key_sent_ten_times_at_once_charges_once(self):
        key = new_key()
        outcomes = run_at_once(
            lambda: services.transfer(self.alice, "bob@x.com", Decimal("10"), key), 10
        )
        self.assertTrue(all(o[0] == "ok" for o in outcomes), outcomes)
        self.assertEqual(sum(1 for o in outcomes if not o[1].replayed), 1)
        self.assertEqual(Wallet.objects.get(user=self.alice).balance, Decimal("90"))

    def test_opposite_transfers_do_not_deadlock(self):
        services.top_up(self.bob, Decimal("100"), new_key())

        def a_to_b():
            return services.transfer(self.alice, "bob@x.com", Decimal("1"), new_key())

        def b_to_a():
            return services.transfer(self.bob, "alice@x.com", Decimal("1"), new_key())

        outcomes = run_at_once(lambda: (a_to_b(), b_to_a()), 10)
        self.assertTrue(all(o[0] == "ok" for o in outcomes), outcomes)
        total = sum(w.balance for w in Wallet.objects.all())
        self.assertEqual(total, Decimal("200"))  # no money created or lost

    def test_request_paid_from_five_tabs_at_once_is_paid_once(self):
        """Different idempotency keys, same request: the request row lock makes only one win."""
        req = services.create_request(self.bob, "alice@x.com", Decimal("30"))
        outcomes = run_at_once(lambda: services.pay_request(self.alice, req.id, new_key()), 5)
        successes = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertTrue(all(isinstance(o[1], services.RequestNotPending) for o in outcomes if o[0] == "error"))
        self.assertEqual(Wallet.objects.get(user=self.alice).balance, Decimal("70"))
        self.assertEqual(Wallet.objects.get(user=self.bob).balance, Decimal("30"))

    def test_pay_and_cancel_race_has_exactly_one_winner(self):
        """Alice pays while Bob cancels, at the same instant. Exactly one must win,
        and money moves if and only if the request ends up PAID."""
        for _ in range(5):
            bob_before = Wallet.objects.get(user=self.bob).balance
            req = services.create_request(self.bob, "alice@x.com", Decimal("1"))
            outcomes = run_together([
                lambda: services.pay_request(self.alice, req.id, new_key()),
                lambda: services.cancel_request(self.bob, req.id),
            ])
            self.assertEqual(sum(1 for o in outcomes if o[0] == "ok"), 1, outcomes)
            req.refresh_from_db()
            moved = Wallet.objects.get(user=self.bob).balance - bob_before
            self.assertEqual(moved, Decimal("1") if req.status == "PAID" else Decimal("0"))


def slow_lock_wallets(real):
    """Pause before locking wallets so concurrent threads truly overlap.
    Without the order-row lock, both threads would then read stale order data."""
    import time

    def wrapper(*ids):
        time.sleep(0.2)
        return real(*ids)

    return wrapper


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row locks")
class MerchantAndHoldConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from merchants import services as merchant_services

        old = timezone.now() - timedelta(days=30)
        self.shop_owner = User.objects.create_user("shop@x.com", "shop@x.com", "pw", first_name="Shop", date_joined=old)
        self.customer = User.objects.create_user("cust@x.com", "cust@x.com", "pw", first_name="Cust", date_joined=old)
        self.newbie = User.objects.create_user("new@x.com", "new@x.com", "pw", first_name="New")
        for user in (self.shop_owner, self.customer, self.newbie):
            Wallet.objects.create(user=user)
        services.top_up(self.customer, Decimal("1000"), new_key())
        services.top_up(self.newbie, Decimal("10000"), new_key())
        self.merchant, _ = merchant_services.create_merchant(self.shop_owner, "Shop")
        self.ms = merchant_services

    def test_checkout_paid_from_five_tabs_at_once_is_paid_once(self):
        order = self.ms.create_order(self.merchant, Decimal("100"), "Tea")
        with patch("merchants.services.lock_wallets", slow_lock_wallets(services.lock_wallets)):
            outcomes = run_at_once(lambda: self.ms.pay_order(self.customer, order.id, new_key()), 5)
        self.assertEqual(sum(1 for o in outcomes if o[0] == "ok"), 1, outcomes)
        self.assertEqual(Wallet.objects.get(user=self.customer).balance, Decimal("900"))

    def test_two_refunds_that_together_exceed_the_order(self):
        """₹60 + ₹60 on a ₹100 order, at the same instant: only one can succeed."""
        order = self.ms.create_order(self.merchant, Decimal("100"), "Tea")
        self.ms.pay_order(self.customer, order.id, new_key())
        # Plenty of merchant balance, so only the order lock can stop a double refund.
        services.top_up(self.shop_owner, Decimal("1000"), new_key())
        with patch("merchants.services.lock_wallets", slow_lock_wallets(services.lock_wallets)):
            outcomes = run_at_once(
                lambda: self.ms.refund_order(self.merchant, order.id, Decimal("60"), "", new_key()), 2
            )
        self.assertEqual(sum(1 for o in outcomes if o[0] == "ok"), 1, outcomes)
        order.refresh_from_db()
        self.assertEqual(order.refunded_amount, Decimal("60"))
        self.assertEqual(Wallet.objects.get(user=self.customer).balance, Decimal("960"))

    def test_approve_and_reject_clicked_at_the_same_instant(self):
        from wallets.models import PaymentHold

        services.transfer(self.newbie, "cust@x.com", Decimal("6000"), new_key())  # new account: held
        hold = PaymentHold.objects.get()
        outcomes = run_together([
            lambda: services.release_hold(hold.id),
            lambda: services.reject_hold(hold.id),
        ])
        self.assertEqual(sum(1 for o in outcomes if o[0] == "ok"), 1, outcomes)
        hold.refresh_from_db()
        customer_got = Wallet.objects.get(user=self.customer).balance - Decimal("1000")
        newbie_has = Wallet.objects.get(user=self.newbie).balance
        if hold.status == "RELEASED":
            self.assertEqual((customer_got, newbie_has), (Decimal("6000"), Decimal("4000")))
        else:
            self.assertEqual((customer_got, newbie_has), (Decimal("0"), Decimal("10000")))
        total = sum(w.balance for w in Wallet.objects.all())
        self.assertEqual(total, Decimal("11000"))  # nothing created or lost, holding wallet included
