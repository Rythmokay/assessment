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

## Deploy on Render (optional)

The brief only needs the local setup above. For a live demo of Section 1 there is a free Render setup.

1. Render dashboard → **New → Blueprint** → connect this repo → pick the **`main`** branch.
2. Render reads [`render.yaml`](render.yaml) and creates a free Web Service and a free Key Value (Redis).
   Click **Apply** and wait for the first deploy (about 3–5 minutes).
3. Open `https://<your-service>.onrender.com/api/orders/summary/` (log in as `demo` / `demo-password`),
   `/api/orders/summary/before-fix/` and `/silk/`.

How it runs (`start.sh`, `SERVICE_ROLE=all`): migrate, collect static, seed the demo user, then start the
Celery worker in the background and gunicorn in the foreground, in one free instance.

Free-plan limits:
- The service sleeps after ~15 minutes idle; the first request after that takes about a minute.
- No persistent disk, so SQLite (and silk's history) resets on every restart. The seed step recreates the
  demo data. Set `DATABASE_URL` to a Postgres URL (Render Postgres, Supabase) to keep data.
- The worker sleeps with the service; queued emails wait in Redis (`acks_late`) rather than being lost.
- Free instances have no Shell, so the Section 2 burst demo is easiest to run locally (see above).

For a production-shaped setup, give the API `SERVICE_ROLE=web` and add a paid Background Worker
running the same `sh start.sh` with `SERVICE_ROLE=worker`.

## Recording (optional section)

Not included.
