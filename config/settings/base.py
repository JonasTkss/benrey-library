"""Settings shared by every environment.

Environment-specific choices (debug, storage backend, security headers) live in
dev.py / prod.py / test.py. Everything that can differ between deploys is read
from environment variables; nothing secret has a default here.
"""

import os
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# A local .env is a developer convenience only. Production never reads one, so
# a stray .env baked into an image cannot silently supply config.
if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.prod":
    environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("SECRET_KEY", default="")
DEBUG = False
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Served from a configurable path. This is obscurity, not security: it only
# keeps drive-by scanners off the login form. Real protection is auth + axes.
ADMIN_URL = env("ADMIN_URL", default="admin/")
if not ADMIN_URL.endswith("/") or ADMIN_URL.startswith("/"):
    raise ImproperlyConfigured("ADMIN_URL must look like 'something/' (no leading slash).")

INSTALLED_APPS = [
    # Unfold must come before django.contrib.admin: it replaces the admin site
    # and templates with a modern, sidebar-based theme on top of the same admin.
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "axes",
    "csp",
    "library",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    # Must be last: it inspects the outcome of the login attempt.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

# --- Database -------------------------------------------------------------
# No default on purpose: a missing DATABASE_URL must fail loudly, never fall
# back to a local SQLite file.
DATABASES = {
    "default": env.db_url("DATABASE_URL", default=None) or {},
}
if not DATABASES["default"]:
    raise ImproperlyConfigured("DATABASE_URL is not set. See .env.example.")
# Keep connections open between requests, but check them before reuse so a
# connection the DB provider dropped is replaced instead of erroring.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# DB-backed cache: shared by all gunicorn workers (unlike locmem), which the
# assistant rate limiter relies on. Table created by `createcachetable`.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}

# --- Auth -----------------------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# The admin is the only UI, so a login without ?next= lands on its dashboard
# (Unfold's login form does not default to it the way Django's does).
LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "admin:index"

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 60 * 60 * 8  # one working day, not Django's 2-week default
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# django-axes: 5 failed logins for the same username from the same IP locks
# that combination for 15 minutes.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25  # hours
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
# Which address counts as "the client" (see config/client_ip.py). Off locally,
# where there is no proxy; prod.py turns it on behind Railway's edge.
AXES_CLIENT_IP_CALLABLE = "config.client_ip.client_ip"
TRUST_X_REAL_IP = False

# --- Security headers shared by all environments ---------------------------
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# --- Uploads --------------------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440  # 2.5 MB request body
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440

# --- Static & media -------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

USE_LOCAL_STORAGE = env.bool("USE_LOCAL_STORAGE", default=False)
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

S3_ENV_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_STORAGE_BUCKET_NAME",
    "AWS_S3_ENDPOINT_URL",
)


def s3_storage_options():
    """S3 options for django-storages, or raise if any required var is missing."""
    missing = [name for name in S3_ENV_VARS if not env(name, default="")]
    if missing:
        raise ImproperlyConfigured(
            "Object storage is not configured. Missing: "
            + ", ".join(missing)
            + ". Set them, or USE_LOCAL_STORAGE=1 in development only."
        )
    return {
        "access_key": env("AWS_ACCESS_KEY_ID"),
        "secret_key": env("AWS_SECRET_ACCESS_KEY"),
        "bucket_name": env("AWS_STORAGE_BUCKET_NAME"),
        "endpoint_url": env("AWS_S3_ENDPOINT_URL"),
        "region_name": env("AWS_S3_REGION_NAME", default=None),
        "addressing_style": env("AWS_S3_ADDRESSING_STYLE", default="path"),
        "signature_version": "s3v4",
        # Private bucket; every image URL is a short-lived signed URL.
        "default_acl": None,
        "querystring_auth": True,
        "querystring_expire": 3600,
        "file_overwrite": False,
    }


STATIC_STORAGE = {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}
LOCAL_MEDIA_STORAGE = {"BACKEND": "django.core.files.storage.FileSystemStorage"}

# --- I18N -----------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Tallinn"
USE_I18N = True
USE_TZ = True

# --- Domain settings ------------------------------------------------------
MAX_ACTIVE_LOANS = env.int("MAX_ACTIVE_LOANS", default=5)

# --- AI assistant ---------------------------------------------------------
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
OPENROUTER_MODELS = env.list("OPENROUTER_MODELS", default=[])
ASSISTANT_RATE_PER_MINUTE = 10
ASSISTANT_RATE_PER_DAY = 100

# --- Logging --------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Never log request bodies (they can carry passwords).
        "django.request": {"level": "WARNING"},
    },
}

# --- Content Security Policy (django-csp 4) -------------------------------
# No inline scripts anywhere: the chat widget is a static JS file. Django's
# admin templates still use a few inline style="" attributes, so styles allow
# 'unsafe-inline'; style injection is a far smaller risk than script injection.
_storage_origin = env("AWS_S3_ENDPOINT_URL", default="").split("/storage/")[0].rstrip("/")
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        # 'unsafe-eval' is required by Alpine.js, which the Unfold admin theme
        # uses to evaluate its x-data attributes. Scripts still load only from
        # our own origin and no inline <script> is allowed.
        "script-src": ["'self'", "'unsafe-eval'"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "img-src": ["'self'", "data:"] + ([_storage_origin] if _storage_origin else []),
        "connect-src": ["'self'"],
        "font-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
}

# --- Admin theme (django-unfold) ------------------------------------------
# Sidebar items are filtered per user: an item whose permission callback
# returns False is not rendered, so each role sees only its own menu.
UNFOLD = {
    "SITE_TITLE": "Benrey Library",
    "SITE_HEADER": "Benrey Library",
    "SITE_SUBHEADER": "Circulation & catalogue",
    "SITE_SYMBOL": "local_library",
    "SITE_URL": "/",
    "SHOW_VIEW_ON_SITE": False,
    "SHOW_HISTORY": True,
    "BORDER_RADIUS": "8px",
    "COLORS": {
        # Teal primary, close to a library-green.
        "primary": {
            "50": "oklch(98.4% .014 180.72)",
            "100": "oklch(95.3% .051 180.801)",
            "200": "oklch(91% .096 180.426)",
            "300": "oklch(85.5% .138 181.071)",
            "400": "oklch(77.7% .152 181.912)",
            "500": "oklch(70.4% .14 182.503)",
            "600": "oklch(60% .118 184.704)",
            "700": "oklch(51.1% .096 186.391)",
            "800": "oklch(43.7% .078 188.216)",
            "900": "oklch(38.6% .063 188.416)",
            "950": "oklch(27.7% .046 192.524)",
        },
    },
    "DASHBOARD_CALLBACK": "library.dashboard.dashboard_callback",
    "SITE_VIEWS": [
        ("guide/", "guide", "library.dashboard.GuideView"),
    ],
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": "library.dashboard.sidebar_navigation",
    },
}
