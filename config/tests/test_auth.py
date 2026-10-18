import pytest
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.urls import reverse

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-test-password-123"


@pytest.fixture
def staff(db):
    return User.objects.create_user("desk", password=PASSWORD, is_staff=True)


def login(client, password):
    return client.post(
        reverse("admin:login"),
        {"username": "desk", "password": password, "next": reverse("admin:index")},
        REMOTE_ADDR="203.0.113.7",
    )


def test_password_login_rotates_session_key(client, staff):
    client.get(reverse("admin:login"))
    client.session.save()
    before = client.session.session_key
    response = login(client, PASSWORD)
    assert response.status_code == 302
    assert client.session.session_key != before  # no session fixation


def test_logout_deletes_session_server_side(client, staff):
    login(client, PASSWORD)
    key = client.session.session_key
    assert Session.objects.filter(session_key=key).exists()
    client.post(reverse("admin:logout"))
    assert not Session.objects.filter(session_key=key).exists()


def test_lockout_after_five_failures(client, staff):
    for _ in range(4):
        assert login(client, "wrong-password").status_code == 200
    assert login(client, "wrong-password").status_code == 429  # fifth failure locks
    # Still locked, even with the right password.
    response = login(client, PASSWORD)
    assert response.status_code == 429
    assert "_auth_user_id" not in client.session


def test_session_cookie_settings(settings):
    assert settings.SESSION_COOKIE_HTTPONLY
    assert settings.SESSION_COOKIE_AGE == 8 * 60 * 60
    assert settings.SESSION_ENGINE == "django.contrib.sessions.backends.db"
