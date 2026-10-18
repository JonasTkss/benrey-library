"""Settings for pytest. Fast hasher, in-memory storage, no network."""

from .base import *  # noqa: F403

SECRET_KEY = "test-only-key"  # noqa: S105 - test settings, not a real secret
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # speed only

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

OPENROUTER_API_KEY = "test-key"
OPENROUTER_MODELS = ["test/model-a:free", "test/model-b:free"]
AXES_ENABLED = True

# Keep test output readable: tests deliberately trigger 403s and lockouts.
LOGGING = LOGGING | {"root": {"handlers": ["console"], "level": "ERROR"}}  # noqa: F405
LOGGING["loggers"] = {"django.request": {"level": "CRITICAL"}, "axes": {"level": "ERROR"}}
