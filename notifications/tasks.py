import json
import logging
import random
import time
import uuid

import redis
from celery import shared_task
from django.conf import settings

from .provider import PermanentEmailError, get_provider
from .rate_limiter import email_rate_limiter, get_redis

logger = logging.getLogger(__name__)

SENT_KEY = "email:sent:{}"
SENT_MARKER_TTL = 7 * 24 * 3600
DEAD_LETTER_KEY = "email:dead_letter"


def enqueue_email(to, subject, body, job_id=None):
    job_id = job_id or uuid.uuid4().hex
    send_email.apply_async(
        kwargs={"job_id": job_id, "to": to, "subject": subject, "body": body}
    )
    return job_id


def backoff_seconds(attempt):
    """Exponential backoff with "equal jitter": half fixed, half random.

    attempt 1 -> 1-2s, 2 -> 2-4s, 3 -> 4-8s ... with the default 2s base,
    capped at EMAIL_BACKOFF_MAX_SECONDS. The random half stops jobs that
    failed together (provider outage) from retrying in lockstep.
    """
    delay = min(
        settings.EMAIL_BACKOFF_MAX_SECONDS,
        settings.EMAIL_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1),
    )
    return delay / 2 + random.uniform(0, delay / 2)


def dead_letter(job, attempt, error):
    entry = {**job, "attempts": attempt, "error": error, "failed_at": time.time()}
    get_redis(settings.EMAIL_REDIS_URL).lpush(DEAD_LETTER_KEY, json.dumps(entry))
    logger.error("email %s dead-lettered after %s attempt(s): %s", job["job_id"], attempt, error)


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=None)
def send_email(self, *, job_id, to, subject, body, attempt=1):
    """Send one email through the provider without exceeding its rate limit.

    `attempt` counts provider failures only. Being throttled by the rate
    limiter is not a failure and reschedules the same attempt, so a flash-sale
    backlog cannot push healthy jobs into the dead-letter list.
    """
    job = {"job_id": job_id, "to": to, "subject": subject, "body": body}
    r = get_redis(settings.EMAIL_REDIS_URL)

    # acks_late means a redelivered message can run again after a crash;
    # skip jobs we already know were delivered.
    if r.exists(SENT_KEY.format(job_id)):
        logger.info("email %s already sent, skipping duplicate delivery", job_id)
        return

    try:
        decision = email_rate_limiter().acquire()
    except redis.RedisError as exc:
        # Fail closed: without the limiter we cannot prove we are under the
        # provider's limit, so delay instead of sending.
        logger.warning("rate limiter unavailable for %s: %s", job_id, exc)
        raise self.retry(exc=exc, countdown=backoff_seconds(1))

    if not decision.allowed:
        # Spread throttled jobs across the next window instead of having the
        # whole backlog wake up together the moment the oldest slot frees.
        jitter = random.uniform(0, settings.EMAIL_RATE_WINDOW_SECONDS)
        raise self.retry(countdown=decision.retry_after + jitter)

    try:
        get_provider().send(to=to, subject=subject, body=body, idempotency_key=job_id)
    except PermanentEmailError as exc:
        dead_letter(job, attempt, repr(exc))
        return
    except Exception as exc:
        if attempt >= settings.EMAIL_MAX_ATTEMPTS:
            dead_letter(job, attempt, repr(exc))
            return
        logger.warning("email %s attempt %s failed, retrying: %s", job_id, attempt, exc)
        raise self.retry(
            exc=exc,
            countdown=backoff_seconds(attempt),
            kwargs={**self.request.kwargs, "attempt": attempt + 1},
        )

    r.set(SENT_KEY.format(job_id), 1, ex=SENT_MARKER_TTL)
