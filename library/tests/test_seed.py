import io

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command

from library.models import Book, Loan, Member

pytestmark = pytest.mark.django_db

STRONG = "correct-horse-battery-staple-9"


def seed(*args):
    call_command("seed_demo", "--small", *args, stdout=io.StringIO())


def test_seed_respects_the_borrowing_rules():
    seed("--seed", "1")
    assert Book.objects.count() == 60
    assert Loan.objects.count() > 100
    for book in Book.objects.with_availability():
        assert book.copies_available >= 0
    for member in Member.objects.with_loan_counts():
        assert member.active_loan_count <= settings.MAX_ACTIVE_LOANS
    assert Book.objects.exclude(cover_image="").count() == 3


def test_seed_is_deterministic_and_resettable(settings):
    settings.DEBUG = True  # reset is only allowed freely in development
    seed("--seed", "7")
    first = list(Book.objects.order_by("isbn").values_list("title", flat=True))
    seed("--seed", "7", "--reset")
    assert list(Book.objects.order_by("isbn").values_list("title", flat=True)) == first


def test_seed_refuses_to_run_twice_without_reset():
    seed()
    with pytest.raises(CommandError, match="--reset"):
        seed()


def test_reset_refuses_when_debug_is_off(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="allow-production"):
        seed("--reset")


def test_demo_users_get_roles_from_env(monkeypatch):
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", STRONG)
    monkeypatch.setenv("DEMO_LIBRARIAN_PASSWORD", STRONG + "x")
    seed("--covers", "0")
    librarian = User.objects.get(username="librarian_demo")
    assert librarian.is_staff and not librarian.is_superuser
    assert list(librarian.groups.values_list("name", flat=True)) == ["Librarian"]
    assert librarian.check_password(STRONG + "x")


def test_weak_demo_password_is_rejected(monkeypatch):
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "password")
    with pytest.raises(CommandError, match="too weak"):
        seed("--covers", "0")
