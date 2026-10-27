import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from notifications.provider import FakeEmailProvider
from notifications.rate_limiter import get_redis
from notifications.tasks import DEAD_LETTER_KEY, SENT_KEY


class Command(BaseCommand):
    help = "Print queue, rate-limiter and delivery state from Redis once per second."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        r = get_redis(settings.EMAIL_REDIS_URL)
        window = settings.EMAIL_RATE_WINDOW_SECONDS
        while True:
            now = time.time()
            calls = [json.loads(c) for c in r.lrange(FakeEmailProvider.CALLS_KEY, 0, -1)]
            last_window = sum(1 for c in calls if c["ts"] > now - window)
            self.stdout.write(
                f"{time.strftime('%H:%M:%S')}  "
                f"waiting={r.llen('celery'):<5} "
                f"reserved/unacked={r.hlen('unacked'):<5} "
                f"limiter_slots_used={r.zcard('ratelimit:email-provider')}/{settings.EMAIL_RATE_LIMIT:<5} "
                f"provider_calls_last_{window:.0f}s={last_window:<5} "
                f"failed_calls={sum(not c['ok'] for c in calls):<4} "
                f"sent={sum(1 for _ in r.scan_iter(SENT_KEY.format('*'))):<5} "
                f"dead_letter={r.llen(DEAD_LETTER_KEY)}"
            )
            if opts["once"]:
                return
            time.sleep(1)
