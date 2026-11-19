"""
"Chai & Co.", a pretend online shop that uses "Pay with PockIt".

It plays the part of a MERCHANT'S OWN SERVER: it keeps its own order table and
only marks an order as paid when PockIt's signed webhook says so. That's how
real shops integrate PayPal or Stripe: never trust the browser redirect alone.
"""
from django.db import models

PRODUCTS = {
    "masala-chai": {"name": "Masala chai", "price": "40.00", "blurb": "Ginger, cardamom, a little too much sugar."},
    "filter-coffee": {"name": "Filter coffee", "price": "60.00", "blurb": "South Indian, served in a steel davara."},
    "gift-box": {"name": "Chai gift box", "price": "1250.00", "blurb": "Six loose-leaf blends and a clay kulhad set."},
}


class ShopOrder(models.Model):
    class Status(models.TextChoices):
        WAITING = "WAITING", "Waiting for payment"
        PAID = "PAID", "Paid"
        ON_HOLD = "ON_HOLD", "Payment on hold"
        DECLINED = "DECLINED", "Payment declined"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        REFUNDED = "REFUNDED", "Refunded"

    product = models.CharField(max_length=40)
    product_name = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WAITING)
    pockit_order_id = models.UUIDField(null=True, blank=True, unique=True)
    refunded_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Shop order #{self.id} {self.product_name} ({self.status})"


class ProcessedWebhook(models.Model):
    """Webhooks can arrive more than once; remember which ones we handled."""

    event_id = models.UUIDField(unique=True)
    event_type = models.CharField(max_length=40)
    received_at = models.DateTimeField(auto_now_add=True)
