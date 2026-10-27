import pytest
from django.conf import settings

from notifications.rate_limiter import get_redis


@pytest.fixture
def redis_client():
    client = get_redis(settings.EMAIL_REDIS_URL)
    client.flushdb()
    yield client
    client.flushdb()
