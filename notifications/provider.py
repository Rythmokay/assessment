import json
import time

from django.conf import settings
from django.utils.module_loading import import_string

from .rate_limiter import get_redis


class TransientEmailError(Exception):
    """Provider timeout, 5xx, 429. Worth retrying."""


class PermanentEmailError(Exception):
    """Invalid address, rejected content. Retrying will not help."""


class FakeEmailProvider:
    """Stand-in for the third-party API, used locally and in tests.

    Every call is appended to a Redis list so tests and the demo commands can
    see exactly when the "provider" was hit. Failures are driven by the
    recipient address:

      *+fail-once@*    raises TransientEmailError on the first attempt only
      *+fail-always@*  raises TransientEmailError on every attempt
      *+invalid@*      raises PermanentEmailError
    """

    CALLS_KEY = "fakeprovider:calls"

    def __init__(self):
        self.redis = get_redis(settings.EMAIL_REDIS_URL)

    def send(self, *, to, subject, body, idempotency_key):
        attempt = self.redis.hincrby("fakeprovider:attempts", idempotency_key, 1)
        ok = not ("+fail-always@" in to or "+invalid@" in to or ("+fail-once@" in to and attempt == 1))
        self.redis.rpush(
            self.CALLS_KEY,
            json.dumps({"job_id": idempotency_key, "ts": time.time(), "ok": ok}),
        )
        if "+invalid@" in to:
            raise PermanentEmailError(f"{to} rejected by provider")
        if not ok:
            raise TransientEmailError(f"provider returned 503 for {to} (attempt {attempt})")


def get_provider():
    return import_string(settings.EMAIL_PROVIDER)()
