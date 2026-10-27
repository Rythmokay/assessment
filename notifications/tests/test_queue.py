"""End-to-end test: a real Celery worker consuming from a real Redis broker."""

import json
import time
from collections import defaultdict

import pytest
from celery.contrib.testing.worker import start_worker

from config.celery import app
from notifications.provider import FakeEmailProvider
from notifications.tasks import DEAD_LETTER_KEY, SENT_KEY, enqueue_email

LIMIT = 50
WINDOW = 1.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.2
# Gap between the Redis grant and the provider call, caused by thread
# scheduling inside the worker. Production should keep a matching margin by
# configuring slightly under the provider's limit (see DESIGN.md).
CLOCK_TOLERANCE = 0.05


@pytest.fixture
def worker(settings, redis_client):
    settings.EMAIL_RATE_LIMIT = LIMIT
    settings.EMAIL_RATE_WINDOW_SECONDS = WINDOW
    settings.EMAIL_MAX_ATTEMPTS = MAX_ATTEMPTS
    settings.EMAIL_BACKOFF_BASE_SECONDS = BACKOFF_BASE
    with start_worker(
        app, pool="threads", concurrency=8, perform_ping_check=False, loglevel="WARNING"
    ):
        yield


def wait_until_settled(redis_client, total, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sent = sum(1 for _ in redis_client.scan_iter(SENT_KEY.format("*")))
        dead = redis_client.llen(DEAD_LETTER_KEY)
        if sent + dead >= total:
            return
        time.sleep(0.2)
    pytest.fail(f"only {sent} sent + {dead} dead-lettered of {total} after {timeout}s")


def test_500_jobs_respect_rate_limit_retry_and_are_never_lost(worker, redis_client):
    flaky, broken, normal = set(), set(), set()
    for i in range(500):
        if i % 100 == 0:
            flaky.add(enqueue_email(f"user{i}+fail-once@example.com", "Order", "hi"))
        elif i in (50, 150):
            broken.add(enqueue_email(f"user{i}+fail-always@example.com", "Order", "hi"))
        else:
            normal.add(enqueue_email(f"user{i}@example.com", "Order", "hi"))

    wait_until_settled(redis_client, 500)

    calls = [json.loads(c) for c in redis_client.lrange(FakeEmailProvider.CALLS_KEY, 0, -1)]
    by_job = defaultdict(list)
    for call in calls:
        by_job[call["job_id"]].append(call)

    # No job lost: every job was delivered exactly once or dead-lettered.
    delivered = {job for job, cs in by_job.items() if any(c["ok"] for c in cs)}
    assert delivered == normal | flaky
    assert all(sum(c["ok"] for c in by_job[job]) == 1 for job in delivered)
    dead = [json.loads(e) for e in redis_client.lrange(DEAD_LETTER_KEY, 0, -1)]
    assert {e["job_id"] for e in dead} == broken
    assert all(e["attempts"] == MAX_ATTEMPTS for e in dead)
    assert all(len(by_job[job]) == MAX_ATTEMPTS for job in broken)

    # Intentional failures were retried, after a backoff delay.
    for job in flaky:
        first, second = by_job[job]
        assert (first["ok"], second["ok"]) == (False, True)
        assert second["ts"] - first["ts"] >= BACKOFF_BASE / 2

    # Rate limit: no window of WINDOW seconds ever saw more than LIMIT calls,
    # i.e. call i and call i + LIMIT are always at least WINDOW apart.
    stamps = sorted(c["ts"] for c in calls)
    assert len(stamps) == 498 + len(flaky) + MAX_ATTEMPTS * len(broken)
    gaps = [stamps[i + LIMIT] - stamps[i] for i in range(len(stamps) - LIMIT)]
    assert min(gaps) >= WINDOW - CLOCK_TOLERANCE
