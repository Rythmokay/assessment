from django.core.management.base import BaseCommand

from notifications.tasks import enqueue_email


class Command(BaseCommand):
    help = "Enqueue a flash-sale style burst of emails, with a few that fail on purpose."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=2000)
        parser.add_argument("--fail-once-every", type=int, default=100)
        parser.add_argument("--fail-always", type=int, default=2)

    def handle(self, *args, **opts):
        for i in range(opts["count"]):
            if i < opts["fail_always"]:
                to = f"user{i}+fail-always@example.com"
            elif opts["fail_once_every"] and i % opts["fail_once_every"] == 0:
                to = f"user{i}+fail-once@example.com"
            else:
                to = f"user{i}@example.com"
            enqueue_email(to, "Your order is confirmed", f"Order #{i}")
        self.stdout.write(self.style.SUCCESS(f"Enqueued {opts['count']} emails."))
