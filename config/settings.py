import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Local-only fallback. Real deployments must set DJANGO_SECRET_KEY.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-local-dev-only")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
# Render sets this to the service's public hostname.
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
    CSRF_TRUSTED_ORIGINS = [f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}"]
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# django-silk is the profiler used for the Section 1 evidence. It records every
# request and SQL query into the database, so it is opt-out for tests.
ENABLE_SILK = os.environ.get("ENABLE_SILK", "1") == "1"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "orders",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEBUG:
    # gunicorn doesn't serve static files; WhiteNoise serves admin/silk assets.
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

if ENABLE_SILK:
    INSTALLED_APPS.append("silk")
    MIDDLEWARE.insert(0, "silk.middleware.SilkyMiddleware")
    SILKY_PYTHON_PROFILER = False
    SILKY_META = True

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite locally; Postgres on Railway via DATABASE_URL.
DATABASES = {
    "default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Basic first so unauthenticated browsers get a 401 + WWW-Authenticate login prompt
        # (with Session first, DRF returns a bare 403 and the browser never asks).
        "rest_framework.authentication.BasicAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

# --- Section 2: email queue -------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
# Nothing reads task return values; delivery state lives in email:sent:* and
# the dead-letter list, so don't write a result key per task.
CELERY_TASK_IGNORE_RESULT = True
# Ack only after the task body returns, so a crashed worker leaves the message
# in the broker instead of silently dropping it. See ANSWERS.md, Section 2.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# Each worker process reserves one message at a time; otherwise a crash takes
# a whole prefetched batch into the visibility-timeout wait.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Must exceed the longest countdown a task can be scheduled with (backoff cap
# below), or the Redis transport redelivers ETA tasks that are still waiting.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 900}

EMAIL_REDIS_URL = REDIS_URL
EMAIL_RATE_LIMIT = int(os.environ.get("EMAIL_RATE_LIMIT", "200"))
EMAIL_RATE_WINDOW_SECONDS = float(os.environ.get("EMAIL_RATE_WINDOW_SECONDS", "60"))
EMAIL_MAX_ATTEMPTS = int(os.environ.get("EMAIL_MAX_ATTEMPTS", "5"))
EMAIL_BACKOFF_BASE_SECONDS = float(os.environ.get("EMAIL_BACKOFF_BASE_SECONDS", "2"))
EMAIL_BACKOFF_MAX_SECONDS = float(os.environ.get("EMAIL_BACKOFF_MAX_SECONDS", "300"))
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "notifications.provider.FakeEmailProvider")

