#!/bin/sh
# Render start command. SERVICE_ROLE picks what this instance runs:
#   worker - only the Celery worker (for a separate, paid Background Worker)
#   all    - Celery in the background plus the API (fits Render's free plan)
#   web    - only the API (default)
set -e
if [ "$SERVICE_ROLE" = "worker" ]; then
    exec celery -A config worker -P threads -c 8 -l info
fi

# Without DATABASE_URL this is a fresh SQLite file on every start, so reseed each time.
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py seed_orders

if [ "$SERVICE_ROLE" = "all" ]; then
    celery -A config worker -P threads -c 4 -l info &
fi
exec gunicorn config.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 60
