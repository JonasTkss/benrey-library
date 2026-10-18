"""Production settings must refuse to boot with unsafe config.

Each case imports config.settings.prod in a fresh interpreter, because Django
settings are module-level and can only be evaluated once per process.
"""

import os
import subprocess
import sys

import pytest

GOOD_ENV = {
    "SECRET_KEY": "x" * 60,
    "ALLOWED_HOSTS": "example.com",
    "CSRF_TRUSTED_ORIGINS": "https://example.com",
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
    "AWS_ACCESS_KEY_ID": "k",
    "AWS_SECRET_ACCESS_KEY": "s",
    "AWS_STORAGE_BUCKET_NAME": "b",
    "AWS_S3_ENDPOINT_URL": "https://example.invalid/storage/v1/s3",
}

PROBE = (
    "import django, os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings.prod';"
    "from django.conf import settings;"
    "print(settings.DEBUG, settings.PASSWORD_HASHERS[0].rsplit('.',1)[1],"
    " settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE,"
    " settings.SECURE_SSL_REDIRECT, settings.STORAGES['default']['BACKEND'])"
)


def run_prod(env_overrides):
    # Start from an empty environment so a developer's .env or shell vars
    # cannot make the test pass by accident.
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}  # noqa: S108
    env |= GOOD_ENV | env_overrides
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        check=False,
    )


def test_prod_settings_are_secure():
    result = run_prod({})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "False",
        "Argon2PasswordHasher",
        "True",
        "True",
        "True",
        "storages.backends.s3.S3Storage",
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"SECRET_KEY": None}, "SECRET_KEY"),
        ({"SECRET_KEY": "change-me"}, "SECRET_KEY"),
        ({"DATABASE_URL": None}, "DATABASE_URL"),
        ({"AWS_STORAGE_BUCKET_NAME": None}, "AWS_STORAGE_BUCKET_NAME"),
    ],
)
def test_prod_refuses_to_start_without_required_config(override, message):
    result = run_prod(override)
    assert result.returncode != 0
    assert message in result.stderr
