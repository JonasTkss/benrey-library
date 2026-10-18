import pytest
from django.contrib.auth.models import User
from django.core.cache import caches
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def limits_on(settings):
    settings.RATELIMIT_ENABLED = True
    caches["default"].clear()
    caches["ratelimit"].clear()


def test_login_flood_is_cut_off_per_ip(client, settings):
    settings.RATELIMIT_LOGIN_PER_MINUTE = 3
    url = reverse("admin:login")
    codes = [
        client.post(url, {"username": f"user{i}", "password": "x"}).status_code for i in range(5)
    ]
    assert codes[:3] != [429] * 3
    assert codes[3:] == [429, 429]


def test_page_scraping_is_cut_off_per_ip(client, settings):
    settings.RATELIMIT_PER_MINUTE = 5
    codes = [client.get(reverse("admin:login")).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5
    assert codes[5:] == [429, 429]


def test_other_ips_are_not_affected(client, settings):
    settings.RATELIMIT_PER_MINUTE = 2
    for _ in range(3):
        client.get(reverse("admin:login"), REMOTE_ADDR="10.0.0.1")
    assert client.get(reverse("admin:login"), REMOTE_ADDR="10.0.0.2").status_code == 200


def test_health_check_is_never_limited(client, settings):
    settings.RATELIMIT_PER_MINUTE = 1
    assert all(client.get("/healthz").status_code == 200 for _ in range(5))


def test_assistant_limit_answers_json_with_retry_after(client, settings):
    settings.RATELIMIT_ASSISTANT_PER_MINUTE = 1
    client.get("/assistant/history/")
    response = client.get("/assistant/history/")
    assert response.status_code == 429
    assert response["Retry-After"] == "60"
    assert "Too many requests" in response.json()["error"]


def test_many_usernames_from_one_ip_get_locked(client):
    """Credential stuffing: a different username each try, same IP."""
    User.objects.create_user("real", password="a-long-test-password-123", is_staff=True)
    url = reverse("admin:login")
    for i in range(5):
        client.post(url, {"username": f"guess{i}", "password": "x"}, REMOTE_ADDR="203.0.113.50")
    # Even the right password for a real account is refused from that IP now.
    response = client.post(
        url,
        {"username": "real", "password": "a-long-test-password-123"},
        REMOTE_ADDR="203.0.113.50",
    )
    assert response.status_code == 429


def test_permissions_policy_header(client):
    header = client.get(reverse("admin:login"))["Permissions-Policy"]
    assert "camera=()" in header and "geolocation=()" in header
