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


def failed_login(client, real_ip):
    return client.post(
        reverse("admin:login"),
        {"username": "desk", "password": "wrong"},
        REMOTE_ADDR="100.64.0.3",  # the proxy, same for everyone
        HTTP_X_REAL_IP=real_ip,
    )


def test_lockout_counts_per_real_client_ip_behind_proxy(client, staff, settings):
    """Behind Railway every request comes from a proxy address; the visitor's
    IP is in X-Real-IP. Failures from different visitors must not add up."""
    settings.TRUST_X_REAL_IP = True
    for i in range(6):
        assert failed_login(client, f"81.20.150.{i + 1}").status_code == 200
    # ...but one visitor is still locked after five failures.
    statuses = [failed_login(client, "81.20.150.99").status_code for _ in range(5)]
    assert statuses[-1] == 429


def test_x_real_ip_is_ignored_unless_trusted(client, staff, settings):
    """Without a proxy, a client could forge X-Real-IP to dodge the lockout."""
    settings.TRUST_X_REAL_IP = False
    statuses = [failed_login(client, f"81.20.150.{i + 1}").status_code for i in range(5)]
    assert statuses[-1] == 429


def test_malformed_x_real_ip_falls_back_to_socket_address():
    from django.test import RequestFactory, override_settings

    from config.client_ip import client_ip

    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_REAL_IP="not-an-ip")
    with override_settings(TRUST_X_REAL_IP=True):
        assert client_ip(request) == "10.0.0.1"
