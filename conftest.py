import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import caches
from django.test import Client


def make_staff(username: str, role: str | None) -> User:
    user = User.objects.create_user(username=username, password="pw-for-tests-only", is_staff=True)
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def admin_user(db):
    return make_staff("admin_role", "Admin")


@pytest.fixture
def librarian(db):
    return make_staff("librarian_role", "Librarian")


def logged_in(user) -> Client:
    # A separate Client per role, so tests can compare two roles side by side.
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def admin_client(admin_user):
    return logged_in(admin_user)


@pytest.fixture
def librarian_client(librarian):
    return logged_in(librarian)


@pytest.fixture(autouse=True)
def fresh_caches():
    """Cached dashboard figures, statistics or AI replies must not leak
    between tests."""
    for alias in ("default", "ratelimit"):
        caches[alias].clear()
    yield
