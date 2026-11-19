"""
python manage.py reconcile

Proves no money was lost or created:
  1. every wallet's balance equals the sum of its ledger entries
  2. everything except top-ups sums to zero (each movement is -X and +X)
  3. the system holding wallet holds exactly the payments that are on hold
  4. no order has been refunded more than was paid
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Sum

from merchants.models import Order
from wallets.models import LedgerEntry, PaymentHold, Wallet
from wallets.services import HOLDS_WALLET_NAME


class Command(BaseCommand):
    help = "Verify wallet balances against the ledger."

    def handle(self, *args, **options):
        problems = []
        zero = Decimal("0")

        for wallet in Wallet.objects.select_related("user"):
            ledger_total = wallet.entries.aggregate(t=Sum("amount"))["t"] or zero
            if ledger_total != wallet.balance:
                problems.append(f"{wallet}: balance ₹{wallet.balance} but ledger says ₹{ledger_total}")

        moved = LedgerEntry.objects.exclude(entry_type=LedgerEntry.EntryType.TOPUP).aggregate(t=Sum("amount"))["t"]
        if (moved or zero) != 0:
            problems.append(f"Money movements should sum to ₹0 but sum to ₹{moved}")

        holds_wallet = Wallet.objects.filter(kind=Wallet.Kind.SYSTEM, name=HOLDS_WALLET_NAME).first()
        held = PaymentHold.objects.filter(status=PaymentHold.Status.HELD).aggregate(t=Sum("amount"))["t"] or zero
        if holds_wallet and holds_wallet.balance != held:
            problems.append(f"Holding wallet has ₹{holds_wallet.balance} but open holds total ₹{held}")

        over = Order.objects.filter(refunded_amount__gt=F("amount")).count()
        if over:
            problems.append(f"{over} order(s) refunded more than they were paid")

        if problems:
            for p in problems:
                self.stderr.write(self.style.ERROR(p))
            raise CommandError(f"{len(problems)} problem(s) found.")

        count = Wallet.objects.count()
        self.stdout.write(self.style.SUCCESS(f"All {count} wallets reconcile with the ledger."))
