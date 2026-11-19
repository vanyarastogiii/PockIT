"""
python manage.py seed_demo

Creates demo data so a recruiter can try everything in a minute:

    Customer:   demo@pockit.app        / pockit-demo-2026
    Shop owner: chaiandco@pockit.app   / pockit-demo-2026   (the "Chai & Co." merchant)

The demo customer has a week of history: transfers, a paid request, two shop
orders (one partly refunded), pending requests to try, and one payment that
the risk rules put ON HOLD.

Safe to run many times: each part is only created if it's missing.
All money is moved through the service layer, so the ledger stays consistent.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from demoshop.models import PRODUCTS, ShopOrder
from demoshop.views import MERCHANT_SLUG
from merchants import services as merchant_services
from merchants.models import Merchant, Order, WebhookEvent
from wallets import services
from wallets.models import LedgerEntry, PaymentHold, PaymentRequest, Wallet

User = get_user_model()

PASSWORD = "pockit-demo-2026"
DEMO_EMAIL = "demo@pockit.app"
SHOP_EMAIL = "chaiandco@pockit.app"

FRIENDS = [
    ("Priya Sharma", "priya@pockit.app"),
    ("Rahul Verma", "rahul@pockit.app"),
    ("Neha Iyer", "neha@pockit.app"),
    ("Kabir Singh", "kabir@pockit.app"),
]


def key():
    return str(uuid.uuid4())


class Command(BaseCommand):
    help = "Create demo accounts, a demo shop, and sample history."

    def user(self, name, email):
        """Get or create a user whose account is 60 days old (so it looks lived-in)."""
        user = User.objects.filter(email=email).first()
        if user is None:
            user = User.objects.create_user(username=email, email=email, password=PASSWORD, first_name=name)
            Wallet.objects.create(user=user)
            User.objects.filter(id=user.id).update(date_joined=timezone.now() - timedelta(days=60))
            user.refresh_from_db()
        return user

    def handle(self, *args, **options):
        # Deliver nothing in background threads while seeding: the server may not even be running.
        with override_settings(WEBHOOK_DELIVER_IN_BACKGROUND=False), transaction.atomic():
            created_people = not User.objects.filter(email=DEMO_EMAIL).exists()
            demo = self.user("Aarav Mehta", DEMO_EMAIL)
            priya, rahul, neha, kabir = (self.user(name, email) for name, email in FRIENDS)
            shop_owner = self.user("Meera Pillai", SHOP_EMAIL)

            merchant, api_key = self.ensure_shop(shop_owner)

            if created_people:
                start = timezone.now()
                self.history(demo, priya, rahul, neha, kabir, merchant)
                self.spread_over_last_week(start)
                self.stdout.write(self.style.SUCCESS("Created the demo customer with a week of history."))
            else:
                self.stdout.write("Demo customer already exists.")

        self.stdout.write(self.style.SUCCESS(f"Customer:   {DEMO_EMAIL} / {PASSWORD}"))
        self.stdout.write(self.style.SUCCESS(f"Shop owner: {SHOP_EMAIL} / {PASSWORD}"))
        if api_key:
            self.stdout.write(f"Chai & Co. secret API key (shown once): {api_key}")
        self.stdout.write(f"Demo shop:  {settings.SITE_URL}/shop/")

    # ---------------------------------------------------------------- shop
    def ensure_shop(self, owner):
        merchant = Merchant.objects.filter(slug=MERCHANT_SLUG).first()
        if merchant:
            return merchant, None
        merchant, api_key = merchant_services.create_merchant(
            owner, "Chai & Co.", webhook_url=f"{settings.SITE_URL}/shop/webhooks/pockit"
        )
        Merchant.objects.filter(id=merchant.id).update(slug=MERCHANT_SLUG)
        merchant.refresh_from_db()
        return merchant, api_key

    def shop_purchase(self, customer, merchant, product_code):
        product = PRODUCTS[product_code]
        shop_order = ShopOrder.objects.create(
            product=product_code, product_name=product["name"], amount=Decimal(product["price"])
        )
        order = merchant_services.create_order(
            merchant, shop_order.amount, f"{product['name']} from Chai & Co.",
            merchant_reference=f"chai-{shop_order.id}", return_url=f"/shop/?order={shop_order.id}",
        )
        shop_order.pockit_order_id = order.id
        shop_order.save(update_fields=["pockit_order_id"])
        merchant_services.pay_order(customer, order.id, key())
        return shop_order, order

    # ---------------------------------------------------------------- history
    def history(self, demo, priya, rahul, neha, kabir, merchant):
        for friend in (priya, rahul, neha, kabir):
            services.top_up(friend, Decimal("5000"), key())

        services.top_up(demo, Decimal("3000"), key())
        services.transfer(demo, priya.email, Decimal("450"), key(), "Movie tickets")
        services.transfer(rahul, demo.email, Decimal("1200"), key(), "Goa trip share")
        services.transfer(demo, neha.email, Decimal("180.50"), key(), "Chai and samosa")
        services.transfer(priya, demo.email, Decimal("320"), key(), "Lunch")

        paid = services.create_request(demo, rahul.email, Decimal("600"), "Concert tickets")
        services.pay_request(rahul, paid.id, key())

        # Two shop orders; the gift box is partly refunded.
        coffee, _ = self.shop_purchase(demo, merchant, "filter-coffee")
        gift, gift_order = self.shop_purchase(demo, merchant, "gift-box")
        merchant_services.refund_order(merchant, gift_order.id, Decimal("250"), "One kulhad arrived cracked", key())
        ShopOrder.objects.filter(id=coffee.id).update(status=ShopOrder.Status.PAID)
        ShopOrder.objects.filter(id=gift.id).update(
            status=ShopOrder.Status.PARTIALLY_REFUNDED, refunded_amount=Decimal("250")
        )
        # The demo shop "received" these webhooks while seeding.
        WebhookEvent.objects.filter(merchant=merchant).update(
            status=WebhookEvent.Status.DELIVERED, attempts=1, last_status_code=200, delivered_at=timezone.now()
        )

        # A big first payment to someone new: the risk rules hold it for review.
        services.top_up(demo, Decimal("25000"), key())
        services.transfer(demo, kabir.email, Decimal("12000"), key(), "Laptop, second hand")

        # Left pending so the visitor can try Pay / Decline and Cancel themselves.
        services.create_request(neha, demo.email, Decimal("250"), "Birthday gift for Ananya")
        services.create_request(demo, priya.email, Decimal("400"), "Cab to the airport")

    def spread_over_last_week(self, since):
        """Give the seeded rows realistic dates over the past week (oldest first)."""
        now = timezone.now()
        entries = list(LedgerEntry.objects.filter(created_at__gte=since).order_by("id"))
        references = list(dict.fromkeys(e.reference for e in entries))  # one date per payment
        step = timedelta(days=7) / max(len(references), 1)
        when = {ref: now - timedelta(days=7) + step * i for i, ref in enumerate(references)}
        for entry in entries:
            LedgerEntry.objects.filter(id=entry.id).update(created_at=when[entry.reference])
        recent = now - timedelta(hours=3)
        for model in (PaymentRequest, Order, PaymentHold, WebhookEvent):
            model.objects.filter(created_at__gte=since).update(created_at=recent)
        Order.objects.filter(paid_at__gte=since).update(paid_at=recent)
