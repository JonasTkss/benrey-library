"""Production (Railway). Fails fast on any missing or unsafe configuration."""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import SECRET_KEY, STATIC_STORAGE, env, s3_storage_options

DEBUG = False

KNOWN_BAD_KEYS = {"", "change-me", "dev-only-insecure-key-do-not-use-in-prod", "test-only-key"}
if SECRET_KEY in KNOWN_BAD_KEYS or len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("SECRET_KEY is missing, too short, or a known placeholder.")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set in production.")
# Railway's deploy health probe sends this Host header.
ALLOWED_HOSTS.append("healthcheck.railway.app")

# Uploads always go to the bucket. s3_storage_options() raises if any of the
# bucket variables are missing, so the app refuses to boot without storage.
STORAGES = {
    "default": {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": s3_storage_options()},
    "staticfiles": STATIC_STORAGE,
}

# Railway terminates TLS at its proxy and forwards plain HTTP.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
# The health check is called by Railway over plain HTTP inside its network.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False  # preload is a one-way door; not for a demo domain
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True

AXES_IPWARE_PROXY_COUNT = env.int("AXES_IPWARE_PROXY_COUNT", default=1)
