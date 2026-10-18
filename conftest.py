import pytest
from django.contrib.auth.models import Group, User


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


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def librarian_client(client, librarian):
    client.force_login(librarian)
    return client
