# Full-Stack Developer Assessment

| Section | Where |
|---|---|
| 1. Diagnose a broken system | `orders/`, written answers in [ANSWERS.md](ANSWERS.md#section-1), profiler output in [docs/profiling/silk_report.md](docs/profiling/silk_report.md) |
| 2. Rate-limited job queue | `notifications/`, design in [DESIGN.md](DESIGN.md), SIGKILL answer in [ANSWERS.md](ANSWERS.md#section-2) |
| 3. Architecture review (A and B) | [ANSWERS.md](ANSWERS.md#section-3) |

## Setup

Requires Python 3.11+ and a Redis server on `localhost:6379`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Redis: either use one you already run locally, or
docker compose up -d redis
```

Set `REDIS_URL` if your Redis is somewhere else (default `redis://localhost:6379/0`).

## Run the tests

```bash
pytest
```

Takes about 30 seconds. The queue tests start a real Celery worker in-process against Redis
**database 15**, which they flush before and after (`TEST_REDIS_URL` overrides this).
They don't touch database 0.

## Section 1: summary endpoint

```bash
python manage.py migrate
python manage.py seed_orders          # user "demo" / "demo-password", 250 orders x 4 items
python manage.py profile_summary      # hits both endpoints, writes docs/profiling/silk_report.md
```

To look at it in the browser:

```bash
python manage.py runserver
```

- http://127.0.0.1:8000/api/orders/summary/before-fix/ — the regressed version (log in with basic auth `demo` / `demo-password`)
- http://127.0.0.1:8000/api/orders/summary/ — the fixed version
- http://127.0.0.1:8000/silk/ — django-silk, shows query count and SQL per request

## Section 2: email queue

Terminal 1, the worker:

```bash
celery -A config worker -P threads -c 8 -l info
```

Terminal 2, a live view of Redis (queue length, unacked messages, limiter usage, deliveries, dead letters):

```bash
python manage.py queue_stats
```

Terminal 3, a flash-sale burst:

```bash
python manage.py send_burst --count 2000
```

With the real limit (200/min) the burst takes about 10 minutes to drain. For a quicker demo, start the
worker and `queue_stats` with e.g. `EMAIL_RATE_LIMIT=20 EMAIL_RATE_WINDOW_SECONDS=5`.

`-P threads` is used because sending email is I/O-bound. The prefork pool also works with this code on
Linux; on macOS with Python 3.14 billiard's prefork pool currently fails at startup of each task.

Settings (environment variables): `EMAIL_RATE_LIMIT`, `EMAIL_RATE_WINDOW_SECONDS`, `EMAIL_MAX_ATTEMPTS`,
`EMAIL_BACKOFF_BASE_SECONDS`, `EMAIL_BACKOFF_MAX_SECONDS`, `EMAIL_PROVIDER`.
