from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from library.dashboard import overdue_badge

from .factories import LoanFactory

pytestmark = pytest.mark.django_db


def overdue_loan():
    return LoanFactory(
        loaned_on=timezone.localdate() - timedelta(days=30),
        due_date=timezone.localdate() - timedelta(days=3),
    )


def test_dashboard_shows_circulation_numbers(librarian_client):
    overdue_loan()
    LoanFactory()
    response = librarian_client.get(reverse("admin:index"))
    assert response.status_code == 200
    assert response.context["loan_stats"]["on_loan"] == 2
    assert response.context["loan_stats"]["overdue"] == 1
    assert len(response.context["overdue_loans"]) == 1


def test_dashboard_is_role_aware(librarian_client, admin_client):
    librarian_page = librarian_client.get(reverse("admin:index")).content.decode()
    admin_page = admin_client.get(reverse("admin:index")).content.decode()
    # Catalogue and security shortcuts only for the Admin role.
    assert reverse("admin:library_book_add") not in librarian_page
    assert reverse("admin:library_book_add") in admin_page
    assert "Failed logins" not in librarian_page
    assert "Failed logins" in admin_page


def test_sidebar_hides_administration_from_librarians(librarian_client, admin_client):
    def sidebar_links(client):
        navigation = client.get(reverse("admin:index")).context["sidebar_navigation"]
        return {
            str(item.get("link_callback") or item["link"])
            for group in navigation
            for item in group["items"]
            if item["has_permission"]
        }

    titles = [
        g["title"]
        for g in librarian_client.get(reverse("admin:index")).context["sidebar_navigation"]
    ]
    assert "Administration" not in titles  # no empty heading either

    staff_url = reverse("admin:auth_user_changelist")
    assert staff_url not in sidebar_links(librarian_client)
    assert staff_url in sidebar_links(admin_client)
    assert reverse("admin:library_loan_changelist") in sidebar_links(librarian_client)


def test_overdue_badge():
    from django.core.cache import cache

    assert overdue_badge(None) is None
    overdue_loan()
    assert overdue_badge(None) is None  # cached for up to a minute
    cache.clear()
    assert overdue_badge(None) == "1"


def test_guide_is_role_aware(librarian_client, admin_client):
    librarian_page = librarian_client.get(reverse("admin:guide"))
    assert librarian_page.status_code == 200
    assert "Lend a book" in librarian_page.content.decode()
    assert "Manage the catalogue" not in librarian_page.content.decode()
    assert "Manage the catalogue" in admin_client.get(reverse("admin:guide")).content.decode()


def test_guide_requires_login(client):
    response = client.get(reverse("admin:guide"))
    assert response.status_code == 302
    assert reverse("admin:login") in response["Location"]


def test_dashboard_figures_are_cached_briefly(librarian_client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    LoanFactory()
    with CaptureQueriesContext(connection) as first:
        librarian_client.get(reverse("admin:index"))
    with CaptureQueriesContext(connection) as second:
        response = librarian_client.get(reverse("admin:index"))
    assert len(second.captured_queries) < len(first.captured_queries)
    assert response.context["loan_stats"]["on_loan"] == 1
