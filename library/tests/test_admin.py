from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import Book, Loan, Member

from .factories import AuthorFactory, BookFactory, LoanFactory, MemberFactory

pytestmark = pytest.mark.django_db


def url(name, *args):
    return reverse(f"admin:{name}", args=args)


def action_names(response):
    if response.context["action_form"] is None:  # no actions at all for this user
        return set()
    return {value for value, _ in response.context["action_form"].fields["action"].choices}


# --- Access --------------------------------------------------------------------


def test_anonymous_is_sent_to_login(client):
    response = client.get(url("library_loan_changelist"))
    assert response.status_code == 302
    assert url("login") in response["Location"]


def test_staff_without_role_sees_nothing(client):
    user = User.objects.create_user("norole", password="x", is_staff=True)
    client.force_login(user)
    assert client.get(url("library_book_changelist")).status_code == 403


def test_index_shows_only_models_the_role_can_use(librarian_client, admin_client):
    librarian_index = librarian_client.get(url("index")).content.decode()
    admin_index = admin_client.get(url("index")).content.decode()
    assert url("library_loan_changelist") in librarian_index
    assert url("auth_user_changelist") not in librarian_index
    assert url("auth_user_changelist") in admin_index


# --- Librarian restrictions are enforced, not just hidden --------------------


def test_librarian_cannot_delete_by_url(librarian_client):
    loan = LoanFactory()
    assert librarian_client.get(url("library_loan_delete", loan.pk)).status_code == 403
    assert librarian_client.post(url("library_loan_delete", loan.pk)).status_code == 403
    assert Loan.objects.filter(pk=loan.pk).exists()


def test_librarian_cannot_add_books_by_url(librarian_client):
    assert librarian_client.get(url("library_book_add")).status_code == 403


def test_librarian_has_no_delete_or_suspend_actions(librarian_client):
    MemberFactory()
    LoanFactory()
    loan_actions = action_names(librarian_client.get(url("library_loan_changelist")))
    member_actions = action_names(librarian_client.get(url("library_member_changelist")))
    assert "delete_selected" not in loan_actions
    assert {"mark_returned", "extend_seven_days"} <= loan_actions
    assert "suspend_members" not in member_actions


def test_librarian_sees_catalogue_read_only(librarian_client):
    book = BookFactory()
    response = librarian_client.get(url("library_book_change", book.pk))
    assert response.status_code == 200
    assert not response.context["has_change_permission"]
    assert 'name="_save"' not in response.content.decode()
    response = librarian_client.post(url("library_book_change", book.pk), {"title": "Hacked"})
    assert response.status_code == 403
    book.refresh_from_db()
    assert book.title != "Hacked"


def test_librarian_sees_fewer_book_columns(librarian_client, admin_client):
    BookFactory()
    librarian_cols = librarian_client.get(url("library_book_changelist")).context["cl"].list_display
    admin_cols = admin_client.get(url("library_book_changelist")).context["cl"].list_display
    assert "isbn" not in librarian_cols
    assert "isbn" in admin_cols


def test_librarian_cannot_change_member_status(librarian_client):
    member = MemberFactory()
    response = librarian_client.get(url("library_member_change", member.pk))
    assert "status" not in response.context["adminform"].form.fields
    data = {
        "full_name": member.full_name,
        "email": member.email,
        "status": Member.Status.SUSPENDED,
        "joined_at": member.joined_at.isoformat(),
        **inline_management_data(),
    }
    response = librarian_client.post(url("library_member_change", member.pk), data)
    assert response.status_code == 302
    member.refresh_from_db()
    assert member.status == Member.Status.ACTIVE


# --- Admin role ------------------------------------------------------------------


def test_admin_has_catalogue_and_status_actions(admin_client):
    BookFactory()
    MemberFactory()
    assert {"withdraw_books", "delete_selected"} <= action_names(
        admin_client.get(url("library_book_changelist"))
    )
    assert "suspend_members" in action_names(admin_client.get(url("library_member_changelist")))
    assert admin_client.get(url("library_book_add")).status_code == 200


def test_admin_suspends_members_in_bulk(admin_client):
    members = [MemberFactory() for _ in range(3)]
    response = admin_client.post(
        url("library_member_changelist"),
        {"action": "suspend_members", "_selected_action": [m.pk for m in members]},
    )
    assert response.status_code == 302
    assert Member.objects.filter(status=Member.Status.SUSPENDED).count() == 3


def test_non_superuser_admin_cannot_grant_superuser(admin_client, admin_user):
    response = admin_client.get(url("auth_user_change", admin_user.pk))
    fields = response.context["adminform"].form.fields
    assert "is_superuser" not in fields
    assert "user_permissions" not in fields


# --- Changelists at scale ----------------------------------------------------------


@pytest.mark.parametrize(
    "changelist",
    ["library_book_changelist", "library_member_changelist", "library_loan_changelist"],
)
def test_changelist_query_count_does_not_grow_with_rows(admin_client, changelist):
    """Guards against N+1: 3 rows and 30 rows must cost the same queries."""

    def make_rows(n):
        authors = [AuthorFactory() for _ in range(2)]
        for _ in range(n):
            LoanFactory(book=BookFactory(authors=authors))

    def count_queries():
        with CaptureQueriesContext(connection) as ctx:
            assert admin_client.get(url(changelist)).status_code == 200
        return len(ctx.captured_queries)

    make_rows(3)
    small = count_queries()
    make_rows(27)
    assert count_queries() == small


def test_changelist_filters_and_search_work(admin_client):
    LoanFactory(
        loaned_on=timezone.localdate() - timedelta(days=30),
        due_date=timezone.localdate() - timedelta(days=5),
    )
    BookFactory(title="Dune")
    for name, params in [
        ("library_loan_changelist", {"due": "overdue"}),
        ("library_loan_changelist", {"due": "soon"}),
        ("library_loan_changelist", {"due": "returned_late"}),
        ("library_member_changelist", {"overdue": "yes"}),
        ("library_member_changelist", {"overdue": "no"}),
        ("library_book_changelist", {"availability": "available"}),
        ("library_book_changelist", {"availability": "out"}),
        ("library_book_changelist", {"published": "2015+"}),
        ("library_book_changelist", {"q": "Dune"}),
        ("library_author_changelist", {"q": "Author"}),
    ]:
        assert admin_client.get(url(name), params).status_code == 200, (name, params)


def test_overdue_filter_returns_only_overdue(admin_client):
    late = LoanFactory(
        loaned_on=timezone.localdate() - timedelta(days=30),
        due_date=timezone.localdate() - timedelta(days=5),
    )
    LoanFactory()
    response = admin_client.get(url("library_loan_changelist"), {"due": "overdue"})
    assert list(response.context["cl"].result_list) == [late]


def test_book_detail_pages_render(admin_client):
    book = BookFactory()
    LoanFactory(book=book)
    assert admin_client.get(url("library_book_change", book.pk)).status_code == 200
    assert (
        admin_client.get(url("library_author_change", book.authors.first().pk)).status_code == 200
    )


# --- Custom actions -----------------------------------------------------------------


def test_mark_returned_reports_successes_and_skips(librarian_client):
    open_loans = [LoanFactory() for _ in range(2)]
    done = LoanFactory(status=Loan.Status.RETURNED, returned_at=timezone.now())
    response = librarian_client.post(
        url("library_loan_changelist"),
        {"action": "mark_returned", "_selected_action": [x.pk for x in [*open_loans, done]]},
        follow=True,
    )
    messages = [str(m) for m in response.context["messages"]]
    assert any("2 loan(s) marked as returned" in m for m in messages)
    assert any("1 loan(s) skipped" in m and "already returned" in m for m in messages)
    assert Loan.objects.filter(status=Loan.Status.ACTIVE).count() == 0


def test_extend_action_skips_overdue(librarian_client):
    ok = LoanFactory()
    late = LoanFactory(
        loaned_on=timezone.localdate() - timedelta(days=30),
        due_date=timezone.localdate() - timedelta(days=1),
    )
    old_due = ok.due_date
    response = librarian_client.post(
        url("library_loan_changelist"),
        {"action": "extend_seven_days", "_selected_action": [ok.pk, late.pk]},
        follow=True,
    )
    ok.refresh_from_db()
    assert ok.due_date == old_due + timedelta(days=7)
    assert any("overdue" in str(m) for m in response.context["messages"])


# --- Creating loans through the admin ---------------------------------------------


def loan_form(book, member):
    today = timezone.localdate()
    return {
        "book": book.pk,
        "member": member.pk,
        "loaned_on": today.isoformat(),
        "due_date": (today + timedelta(days=14)).isoformat(),
        "notes": "",
    }


def test_librarian_lends_a_book(librarian_client):
    book, member = BookFactory(), MemberFactory()
    response = librarian_client.post(url("library_loan_add"), loan_form(book, member))
    assert response.status_code == 302
    assert Loan.objects.get().member == member


def test_rule_violation_is_a_form_error_not_a_crash(librarian_client):
    book = BookFactory(copies_total=1)
    LoanFactory(book=book)
    response = librarian_client.post(url("library_loan_add"), loan_form(book, MemberFactory()))
    assert response.status_code == 200
    assert "All 1 copies" in response.content.decode()
    assert Loan.objects.count() == 1


def inline_management_data(rows=None):
    """Management form data for the two Loan inlines on the member page."""
    rows = rows or []
    data = {
        "loans-TOTAL_FORMS": str(len(rows)),
        "loans-INITIAL_FORMS": "0",
        "loans-MIN_NUM_FORMS": "0",
        "loans-MAX_NUM_FORMS": "1000",
        "loans-2-TOTAL_FORMS": "0",
        "loans-2-INITIAL_FORMS": "0",
        "loans-2-MIN_NUM_FORMS": "0",
        "loans-2-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        data |= {f"loans-{i}-{k}": v for k, v in row.items()}
    return data


def member_form(member, rows=None):
    return {
        "full_name": member.full_name,
        "email": member.email,
        "joined_at": member.joined_at.isoformat(),
        **inline_management_data(rows),
    }


def test_lend_inline_creates_loan_through_service(librarian_client):
    member, book = MemberFactory(), BookFactory()
    due = (timezone.localdate() + timedelta(days=10)).isoformat()
    response = librarian_client.post(
        url("library_member_change", member.pk),
        member_form(member, [{"book": book.pk, "due_date": due, "notes": "worn spine"}]),
    )
    assert response.status_code == 302, response.context["errors"]
    loan = Loan.objects.get()
    assert (loan.member, loan.book, loan.notes) == (member, book, "worn spine")


def test_lend_inline_shows_rule_errors(librarian_client):
    member = MemberFactory(status=Member.Status.SUSPENDED)
    due = (timezone.localdate() + timedelta(days=10)).isoformat()
    response = librarian_client.post(
        url("library_member_change", member.pk),
        member_form(member, [{"book": BookFactory().pk, "due_date": due}]),
    )
    assert response.status_code == 200
    assert "suspended" in response.content.decode()
    assert not Loan.objects.exists()


def test_existing_loan_cannot_be_edited_except_notes(librarian_client):
    loan = LoanFactory()
    response = librarian_client.get(url("library_loan_change", loan.pk))
    assert set(response.context["adminform"].form.fields) == {"notes"}


def test_book_copies_cannot_drop_below_loans(admin_client):
    book = BookFactory(copies_total=2)
    LoanFactory(book=book)
    LoanFactory(book=book)
    data = {
        "title": book.title,
        "authors": [a.pk for a in book.authors.all()],
        "isbn": book.isbn,
        "genre": book.genre,
        "copies_total": 1,
        "loans-TOTAL_FORMS": "0",
        "loans-INITIAL_FORMS": "0",
    }
    response = admin_client.post(url("library_book_change", book.pk), data)
    assert response.status_code == 200
    assert Book.objects.get(pk=book.pk).copies_total == 2


def test_cover_upload_through_admin_is_stored_under_random_name(admin_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from .test_validators import png_bytes

    author = AuthorFactory()
    data = {
        "title": "Uploaded",
        "authors": [author.pk],
        "isbn": "978-0-13-468599-1",
        "genre": "fiction",
        "copies_total": 1,
        "cover_image": SimpleUploadedFile("My Cover.png", png_bytes(), "image/png"),
        "loans-TOTAL_FORMS": "0",
        "loans-INITIAL_FORMS": "0",
    }
    response = admin_client.post(url("library_book_add"), data)
    assert response.status_code == 302
    name = Book.objects.get(title="Uploaded").cover_image.name
    assert name.startswith("covers/") and "My Cover" not in name


def test_svg_upload_through_admin_is_rejected(admin_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    data = {
        "title": "Bad",
        "authors": [AuthorFactory().pk],
        "isbn": "978-0-13-468599-1",
        "genre": "fiction",
        "copies_total": 1,
        "cover_image": SimpleUploadedFile("x.svg", b"<svg onload='alert(1)'/>", "image/svg+xml"),
        "loans-TOTAL_FORMS": "0",
        "loans-INITIAL_FORMS": "0",
    }
    response = admin_client.post(url("library_book_add"), data)
    assert response.status_code == 200
    assert not Book.objects.filter(title="Bad").exists()
