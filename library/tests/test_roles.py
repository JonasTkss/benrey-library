import pytest
from django.contrib.auth.models import Group

pytestmark = pytest.mark.django_db


def perms(group_name):
    group = Group.objects.get(name=group_name)
    return {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}


def test_librarian_permissions_are_exactly_circulation():
    assert perms("Librarian") == {
        "library.view_author",
        "library.view_book",
        "library.add_member",
        "library.change_member",
        "library.view_member",
        "library.add_loan",
        "library.change_loan",
        "library.view_loan",
    }


def test_librarian_can_delete_nothing():
    assert not any(".delete_" in p for p in perms("Librarian"))


def test_admin_owns_catalogue_members_and_users():
    admin = perms("Admin")
    for model in ("author", "book", "member", "loan"):
        for action in ("add", "change", "delete", "view"):
            assert f"library.{action}_{model}" in admin
    assert "library.change_member_status" in admin
    assert {"auth.add_user", "auth.change_user", "auth.change_group"} <= admin
