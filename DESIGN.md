# Section 2: Rate-limited email queue

## The problem in numbers

- Provider limit: 200 emails per minute.
- Flash sale: 2,000 requests in under 10 seconds.
- So the queue has to absorb a backlog roughly 10x the per-minute budget and drain it over about
  10 minutes, without ever crossing 200 in a minute, while retrying provider failures and surviving
  worker crashes.

The rate limit is global (it's per provider account), so it has to be enforced in one shared place,
not per worker or per process.

## Choosing the queue

**Celery + Redis**

- Mature retry API (`self.retry(countdown=...)`), ETA scheduling, `acks_late`, worker-lost handling,
  and lots of operational tooling (Flower, `celery inspect`).
- Redis is also the natural place for the rate limiter, so one piece of infrastructure covers both.
- Costs: Redis is not a real message broker. There's no native ack/nack; kombu emulates it with an
  `unacked` hash and a visibility timeout, which has sharp edges (see "Durability" below). Celery has a
  lot of configuration and some defaults are unsafe for this use case (`acks_late=False`).
- Celery's built-in `rate_limit` task option is per worker instance, so it can't enforce a global
  provider limit. A shared limiter is still needed.

**Django Q (django-q2)**

- Simpler to run, can use the Django ORM as broker, has a nice admin integration and scheduled tasks.
- Retries are a fixed `retry` timeout per cluster, not per-task exponential backoff. There's no
  per-task countdown/ETA to reschedule a throttled job, and no built-in dead-letter concept beyond
  "failed tasks" in the database.
- Smaller community; the original project is unmaintained (django-q2 is the fork). For a queue on the
  checkout path, I'd rather depend on something with more people running it in production.

**Custom (Redis lists + a worker loop)**

- Full control: `BLMOVE` from a pending list into a processing list gives you reliable-queue semantics,
  and a delayed sorted set gives you backoff.
- But then I own reliable delivery, the delayed-job scheduler, crash recovery, graceful shutdown,
  concurrency, monitoring and tests for all of it. That is a lot of subtle code to get right and keep
  right, for no product benefit.

**Chosen: Celery + Redis.** The assessment asks for it, but I'd pick it here anyway. The hard parts
(retries, delayed execution, crash recovery) are already solved, and the parts Celery doesn't solve
(global rate limiting, dead-lettering, idempotency) are small and easy to own.

## Flow

```
enqueue_email()  ->  Redis list "celery"  ->  worker: send_email task
                                                 |
                                   already sent? (email:sent:<job_id>) -> skip
                                                 |
                                   limiter.acquire()  (Lua, sliding window)
                                    | denied                      | allowed
                     retry(countdown=retry_after+jitter)    provider.send()
                     (same attempt number)                   | ok        | error
                                                    mark sent      transient: retry with backoff,
                                                                   attempt+1, until EMAIL_MAX_ATTEMPTS
                                                                   permanent / exhausted: dead-letter
```

Code: `notifications/tasks.py`, `notifications/rate_limiter.py`.

## Rate limiter: sliding window log

`notifications/rate_limiter.py`. Each granted send is a member of the sorted set
`ratelimit:email-provider`, scored by grant time in microseconds. To acquire a slot:

1. `ZREMRANGEBYSCORE key -inf (now - window)` drops grants that left the window.
2. `ZCARD key` counts what is left.
3. If under the limit, `ZADD key now <uuid>` and `PEXPIRE key window`; otherwise return how long until
   the oldest grant expires (`ZRANGE key 0 0 WITHSCORES`).

### Why this over the other two

- **Fixed window (INCR + EXPIRE)** is the cheapest, but it allows 2x the limit across a window
  boundary: 200 at 12:00:59 and 200 more at 12:01:00 is 400 in two seconds, which is exactly the kind
  of thing a flash sale produces. If the provider measures a rolling minute, we'd get 429s.
- **Token bucket (DECR + TTL)** smooths traffic and allows bursts up to bucket size. But with a bucket
  of 200 refilling at 200/min, a full bucket plus a minute of refill can put close to 400 sends inside
  one rolling 60s window. To make it strictly safe I'd have to shrink the bucket, which throws away
  the burst capacity that is its main advantage.
- **Sliding window log** gives the exact guarantee I want: no 60-second window, wherever it starts,
  ever contains more than 200 grants. The cost is memory, one ZSET member per send in the window, so
  at most 200 members here. That cost would rule it out for a per-user limiter on millions of users,
  but for one provider-wide key it's nothing.

It also returns a precise `retry_after`, so throttled tasks can be rescheduled for when a slot actually
frees up instead of polling.

### Atomicity

The three steps run in one Lua script (`EVALSHA` via `redis-py`'s `register_script`). Redis runs a
script to completion without interleaving other commands, so two workers can't both see 199 and both
add.

- `MULTI/EXEC` doesn't work for this on its own: commands inside a transaction are queued, so I can't
  branch on the `ZCARD` result before deciding to `ZADD`. It would need `WATCH` and a retry loop, which
  under a burst of hundreds of concurrent workers turns into a storm of aborted transactions.
- A pipeline is just batching, it gives no isolation at all.

The script reads the clock with `redis.call('TIME')` rather than taking `now` from the worker. Workers
on different machines drift; if each used its own clock, the window boundaries would disagree. Since
Redis 5, scripts replicate their effects rather than the script itself, so using `TIME` in a script
that writes is fine.

### When Redis is down: fail closed

If `acquire()` raises a `RedisError`, the task does not send. It schedules a retry. Reasons:

- Exceeding the provider limit isn't a soft failure. Providers answer with 429s and, if it keeps
  happening, can throttle or suspend the account, which then breaks every email, including OTPs.
- In this design Redis is also the Celery broker. If it's really down, no new tasks are being delivered
  anyway, so failing open would only matter for tasks already in flight.

Trade-off: a Redis outage delays emails rather than risking the account. For OTPs, that's a real cost.
If the limiter lived on a separate Redis from the broker, I'd still fail closed but alert on it.

### Headroom

The grant happens in Redis; the HTTP call to the provider happens a few milliseconds later, and the
provider counts at its own receive time. Network jitter can make two grants that were exactly 60s apart
arrive at the provider 59.99s apart. In production I'd set `EMAIL_RATE_LIMIT` slightly under the real
limit (for example 190) to absorb that. The integration test uses an explicit 50ms tolerance for the
same reason, and the limiter unit test checks the exact limit with no tolerance.

## Retries and dead-lettering

- **Two kinds of "try again".** Being throttled is not a failure, it's the system working. Throttled
  tasks are rescheduled with the same `attempt` number. Only provider errors increment `attempt`.
  Otherwise a 2,000-email backlog would exhaust everyone's retry budget just by waiting in line and
  dead-letter healthy jobs. I track `attempt` in the task kwargs rather than relying on
  `self.request.retries`, because Celery increments `retries` on every `self.retry()` call, throttled
  or not.
- **Exponential backoff with jitter.** Delay for attempt *n* is `base * 2^(n-1)`, capped at 300s, and
  half of it is randomised ("equal jitter"). With the 2s default: 1-2s, 2-4s, 4-8s, 8-16s. The jitter
  matters after a provider outage: everything failed at once, and without it everything retries at
  once.
- **Transient vs permanent.** `PermanentEmailError` (invalid address, rejected content) is
  dead-lettered immediately because retrying can't help. Anything else is retried up to
  `EMAIL_MAX_ATTEMPTS` (5).
- **Dead letter.** Exhausted or permanent jobs are `LPUSH`ed as JSON (job, attempts, last error,
  timestamp) to `email:dead_letter` and logged at ERROR. From there they can be inspected and
  re-enqueued with `enqueue_email(...)` using the same `job_id`. I used a Redis list rather than a
  database table to keep everything for the queue in one place; with a real app I'd move this to a
  table so support staff can see it in the admin.

## Durability

Configured in `config/settings.py` and on the task:

- `task_acks_late=True`: the message is acknowledged after the task returns, not when it's received.
- `task_reject_on_worker_lost=True`: if a pool process dies mid-task, the message is requeued instead
  of being marked failed and acked.
- `worker_prefetch_multiplier=1`: a worker doesn't hoard messages it can't process yet.
- `visibility_timeout=900`: how long a message can sit unacked before Redis-transport redelivers it.
  It must be longer than the longest countdown a task can be scheduled with (300s backoff cap, or one
  rate window plus jitter), because a task waiting for its ETA is held unacked in worker memory. If the
  timeout were shorter, kombu would redeliver it while the original is still waiting, and we'd send
  twice.
- **Idempotency.** `acks_late` makes delivery at-least-once. Before sending, the task checks
  `email:sent:<job_id>` and skips if present; after a successful send it sets that key (7 day TTL). The
  `job_id` is also passed to the provider as `idempotency_key`, which closes the remaining gap (crash
  after the provider accepted but before we wrote the marker) if the provider supports it.

What happens on SIGKILL specifically is in ANSWERS.md, Section 2.

## What this doesn't handle well

- **No priority.** An OTP enqueued behind a 2,000-email flash sale waits up to 10 minutes, which makes
  it useless. The fix is a separate `email-priority` queue with its own worker (`-Q email-priority`),
  and either a reserved share of the limit (two limiter keys, e.g. 40 + 160) or letting priority work
  check the limiter first. I haven't built it because the brief doesn't ask for it, but I'd do it
  before production.
- **Throttled-retry churn.** Each throttled task re-checks the limiter about once per window. Draining
  a 2,000 backlog at 200/min costs roughly 2000 + 1800 + ... + 200 ≈ 11,000 limiter calls and the same
  number of re-published messages over 10 minutes, about 20 per second. Redis doesn't notice, but at
  100x the volume I'd switch to a single dispatcher that pulls from a pending list at the allowed rate
  instead of letting every task poll.
- **No FIFO ordering** under throttling. Random jitter means a job enqueued later can be sent earlier.
  Fine for transactional email; not fine if ordering mattered.
- **Redis is a single point of failure** for both the queue and the limiter. With default RDB
  snapshots a Redis crash can lose recently enqueued jobs. I'd run it with `appendonly yes` and
  `appendfsync everysec` (lose at most ~1s), and for "never lose an order confirmation" write the email
  to a database outbox table in the same transaction as the order, with a periodic sweep that
  re-enqueues anything not marked sent.
- **A crashed task's slot is not refunded.** If a worker dies after acquiring a slot but before
  sending, that slot sits unused until it ages out. That errs on the safe side, so I left it.
