#!/bin/sh
# One image, two Railway services: SERVICE_ROLE=worker runs Celery, anything else runs the API.
set -e
if [ "$SERVICE_ROLE" = "worker" ]; then
    exec celery -A config worker -P threads -c 8 -l info
fi
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py seed_orders
exec gunicorn config.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 60
