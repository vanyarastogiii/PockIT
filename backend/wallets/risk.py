"""
Risk rules: decide whether a payment can go straight through, or should be
put ON HOLD for a person to review first.

Each rule is a small function that returns a reason (or None). If any rule
fires, the payment is held. Rules are deliberately simple and explainable,
because a held customer deserves to know why, and an ops person reviewing
the hold needs to see what triggered it.

Real payment companies combine rules like these with machine-learning scores.
The plumbing (hold, review, release or return the money) stays the same.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import LedgerEntry


@dataclass
class Reason:
    code: str
    message: str

    def as_dict(self):
        return {"code": self.code, "message": self.message}


def _rupees(value) -> str:
    return f"₹{Decimal(value):,.0f}"


def _rule_new_account_large(ctx):
    cfg = ctx.cfg
    age = timezone.now() - ctx.sender.date_joined
    limit = Decimal(cfg["NEW_ACCOUNT_MAX_AMOUNT"])
    if age < timedelta(hours=cfg["NEW_ACCOUNT_HOURS"]) and ctx.amount > limit:
        return Reason(
            "new_account_large",
            f"Account is less than {cfg['NEW_ACCOUNT_HOURS']} hours old and sent more than {_rupees(limit)}",
        )


def _rule_velocity(ctx):
    cfg = ctx.cfg
    since = timezone.now() - timedelta(minutes=cfg["VELOCITY_WINDOW_MINUTES"])
    recent = LedgerEntry.objects.filter(
        wallet=ctx.sender_wallet,
        entry_type__in=LedgerEntry.OUTGOING_PAYMENT_TYPES,
        created_at__gte=since,
    ).count()
    if recent >= cfg["VELOCITY_MAX_PAYMENTS"]:
        return Reason(
            "velocity",
            f"More than {cfg['VELOCITY_MAX_PAYMENTS']} payments in {cfg['VELOCITY_WINDOW_MINUTES']} minutes",
        )


def _rule_new_recipient_large(ctx):
    limit = Decimal(ctx.cfg["NEW_RECIPIENT_MAX_AMOUNT"])
    if ctx.amount <= limit:
        return None
    paid_before = LedgerEntry.objects.filter(
        wallet=ctx.sender_wallet,
        entry_type__in=LedgerEntry.OUTGOING_PAYMENT_TYPES,
        counterparty=ctx.recipient_wallet,
    ).exists()
    if not paid_before:
        return Reason("new_recipient_large", f"First payment to this person is more than {_rupees(limit)}")


def _rule_large_amount(ctx):
    limit = Decimal(ctx.cfg["LARGE_PAYMENT_AMOUNT"])
    if ctx.amount >= limit:
        return Reason("large_amount", f"Payment of {_rupees(limit)} or more")


RULES = [_rule_new_account_large, _rule_velocity, _rule_new_recipient_large, _rule_large_amount]


@dataclass
class _Context:
    sender: object
    sender_wallet: object
    recipient_wallet: object
    amount: Decimal
    cfg: dict


def assess(sender, sender_wallet, recipient_wallet, amount: Decimal) -> list[Reason]:
    """Return the reasons to hold this payment. An empty list means 'let it through'."""
    cfg = settings.RISK_RULES
    if not cfg.get("ENABLED", True):
        return []
    ctx = _Context(sender, sender_wallet, recipient_wallet, Decimal(amount), cfg)
    return [reason for rule in RULES if (reason := rule(ctx))]
