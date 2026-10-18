"""No URL in the app hands out data to someone who should not have it.

This walks every route Django knows about (so a route added later is covered
automatically), fills in dummy arguments, and requests it:
- as an anonymous visitor: everything except a short allow-list must redirect
  to the login page or refuse (401/403/404/405);
- as a staff account with no role: no library, user or audit data at all.
"""

import re

import pytest
from django.contrib.auth.models import User
from django.urls import URLPattern, URLResolver, get_resolver

from library.tests.factories import AuthorFactory, BookFactory, LoanFactory, MemberFactory

pytestmark = pytest.mark.django_db

# Reachable without logging in, on purpose.
PUBLIC = {
    "healthz",  # {"status": "ok"} for the platform probe, nothing else
    "admin:login",
    "admin:logout",  # POST-only; logging out an anonymous user is a no-op
}
REFUSED = {301, 302, 401, 403, 404, 405}


def walk(patterns, prefix="", namespace=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            ns = f"{namespace}{pattern.namespace}:" if pattern.namespace else namespace
            yield from walk(pattern.url_patterns, prefix + str(pattern.pattern), ns)
        elif isinstance(pattern, URLPattern):
            name = f"{namespace}{pattern.name}" if pattern.name else None
            yield prefix + str(pattern.pattern), name


def concrete(route, ids):
    """Turn a route pattern into a real path with plausible arguments."""
    path = route.lstrip("^").rstrip("$")
    path = path.replace("(?P<url>.*)", "nonexistent/")
    path = re.sub(r"<path:object_id>", str(ids["object"]), path)
    path = re.sub(r"<path:content_type_id>", str(ids["content_type"]), path)
    path = re.sub(r"<str:name>", "0123456789abcdef0123456789abcdef.png", path)
    path = re.sub(r"<[^>]+>", "1", path)
    return "/" + path


@pytest.fixture
def data():
    book = BookFactory(authors=[AuthorFactory()])
    member = MemberFactory()
    loan = LoanFactory(book=book, member=member)
    from django.contrib.contenttypes.models import ContentType

    return {"object": loan.pk, "content_type": ContentType.objects.get_for_model(loan).pk}


def all_routes():
    return list(walk(get_resolver().url_patterns))


def test_route_walk_finds_the_whole_app():
    routes = all_routes()
    assert len(routes) > 50  # every admin model URL plus our own
    names = {name for _, name in routes}
    assert {"assistant:ask", "cover", "admin:guide", "admin:library_member_changelist"} <= names


@pytest.mark.parametrize("method", ["get", "post"])
def test_anonymous_visitors_get_nothing(client, data, method):
    leaks, checked = [], 0
    for route, name in all_routes():
        if name in PUBLIC or route in ("",):
            continue
        path = concrete(route, data)
        checked += 1
        response = getattr(client, method)(path)
        if response.status_code not in REFUSED:
            leaks.append((method, path, response.status_code))
        elif response.status_code in (301, 302):
            # A redirect must go to the login page, not to content.
            assert "login" in response["Location"], (path, response["Location"])
    assert checked > 50
    assert leaks == []


def test_staff_without_a_role_sees_no_data(client, data):
    user = User.objects.create_user("norole", password="x", is_staff=True)
    client.force_login(user)
    leaks = []
    for route, _name in all_routes():
        if not route.startswith(("admin/library", "admin/auth", "admin/axes")):
            continue
        path = concrete(route, data)
        response = client.get(path)
        if response.status_code == 200:
            leaks.append(path)
    assert leaks == []


def test_staff_without_a_role_cannot_use_the_assistant_lookups(client, data):
    import json

    user = User.objects.create_user("norole2", password="x", is_staff=True)
    client.force_login(user)
    from assistant import lookups

    assert lookups.allowed_lookups(user) == []
    response = client.post(
        "/assistant/ask/",
        json.dumps({"question": "who has the highest bill?"}),
        content_type="application/json",
    )
    assert response.json().get("results", []) == []


def test_health_check_reveals_nothing(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_home_page_only_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    follow = client.get(response["Location"])
    assert follow.status_code == 302 and "login" in follow["Location"]


def test_csrf_failure_is_a_short_403(librarian):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(librarian)
    api = strict.post("/assistant/history/clear/")
    assert api.status_code == 403 and "session expired" in api.json()["error"]
    page = strict.post("/" + __import__("django.conf").conf.settings.ADMIN_URL + "logout/")
    assert page.status_code == 403
    assert b"Referer" not in page.content  # not Django's long explanation
