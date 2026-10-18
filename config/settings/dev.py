"""Local development. Never used on the deployed app."""

from .base import *  # noqa: F403
from .base import LOCAL_MEDIA_STORAGE, STATIC_STORAGE, USE_LOCAL_STORAGE, env, s3_storage_options

DEBUG = True
SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key-do-not-use-in-prod")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Local disk for uploads only when explicitly asked for, never as a silent
# fallback when the bucket config is missing.
STORAGES = {
    "default": LOCAL_MEDIA_STORAGE
    if USE_LOCAL_STORAGE
    else {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": s3_storage_options()},
    "staticfiles": STATIC_STORAGE,
}
