"""
python manage.py deliver_webhooks          # deliver everything that's due, once
python manage.py deliver_webhooks --loop   # keep running (a simple worker process)

Webhooks are normally sent right after the payment. This command retries the
ones that failed (merchant server down, timeout...) with exponential backoff.
"""
import time

from django.core.management.base import BaseCommand

from merchants import webhooks


class Command(BaseCommand):
    help = "Deliver pending webhook events (retries with backoff)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, checking every few seconds.")
        parser.add_argument("--interval", type=int, default=5)

    def handle(self, *args, loop=False, interval=5, **options):
        while True:
            tried = webhooks.deliver_due()
            if tried:
                self.stdout.write(f"Tried {tried} webhook(s).")
            if not loop:
                break
            time.sleep(interval)
