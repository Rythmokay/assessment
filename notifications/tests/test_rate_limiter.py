import time
from concurrent.futures import ThreadPoolExecutor

from notifications.rate_limiter import SlidingWindowRateLimiter


def test_concurrent_callers_never_get_more_than_the_limit(redis_client):
    limiter = SlidingWindowRateLimiter(redis_client, "test:limit", limit=20, window_seconds=5)

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _: limiter.acquire(), range(200)))

    granted = [d for d in decisions if d.allowed]
    denied = [d for d in decisions if not d.allowed]
    assert len(granted) == 20
    assert len(denied) == 180
    assert all(0 < d.retry_after <= 5 for d in denied)


def test_slots_free_up_as_the_window_slides(redis_client):
    limiter = SlidingWindowRateLimiter(redis_client, "test:slide", limit=3, window_seconds=0.5)

    assert [limiter.acquire().allowed for _ in range(4)] == [True, True, True, False]

    time.sleep(0.55)
    assert limiter.acquire().allowed


def test_key_expires_so_idle_limiters_do_not_leak_memory(redis_client):
    limiter = SlidingWindowRateLimiter(redis_client, "test:ttl", limit=5, window_seconds=2)
    limiter.acquire()

    assert 0 < redis_client.pttl("test:ttl") <= 2000
