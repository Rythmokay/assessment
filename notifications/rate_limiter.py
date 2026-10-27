"""Sliding-window-log rate limiter backed by a Redis sorted set.

Each granted send is a member of a ZSET scored by its grant time in
microseconds. To decide on a new request we drop entries older than the
window (ZREMRANGEBYSCORE), count what is left (ZCARD) and only ZADD if we are
under the limit. The three steps run inside one Lua script, so Redis executes
them atomically: no other client can interleave between the count and the add.

Time comes from Redis TIME inside the script rather than from the worker, so
workers on different hosts with drifting clocks still agree on one timeline.
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache

import redis
from django.conf import settings

_ACQUIRE = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_us = tonumber(ARGV[2])
local member = ARGV[3]

local t = redis.call('TIME')
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_us - window_us)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now_us, member)
  redis.call('PEXPIRE', key, math.ceil(window_us / 1000))
  return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return {0, tonumber(oldest[2]) + window_us - now_us}
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float  # seconds until the oldest grant leaves the window


class SlidingWindowRateLimiter:
    def __init__(self, client, key, limit, window_seconds):
        self.key = key
        self.limit = limit
        self.window_us = int(window_seconds * 1_000_000)
        self._script = client.register_script(_ACQUIRE)

    def acquire(self):
        """Try to take one slot. Raises redis.RedisError if Redis is unreachable."""
        allowed, retry_after_us = self._script(
            keys=[self.key], args=[self.limit, self.window_us, uuid.uuid4().hex]
        )
        return Decision(bool(allowed), max(retry_after_us, 0) / 1_000_000)


@lru_cache(maxsize=None)
def get_redis(url):
    return redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)


def email_rate_limiter():
    return SlidingWindowRateLimiter(
        get_redis(settings.EMAIL_REDIS_URL),
        key="ratelimit:email-provider",
        limit=settings.EMAIL_RATE_LIMIT,
        window_seconds=settings.EMAIL_RATE_WINDOW_SECONDS,
    )
