import os

os.environ["ENABLE_SILK"] = "0"

from .settings import *  # noqa: E402,F401,F403

# Tests use a dedicated Redis database that is flushed between runs.
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
EMAIL_REDIS_URL = REDIS_URL

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
