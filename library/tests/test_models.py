from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from library.models import Book, Loan, Member

from .factories import BookFactory, LoanFactory, MemberFactory

pytestmark = pytest.mark.django_db

TODAY = date(2026, 3, 20)


class TestComputedLoanValues:
    @pytest.fixture(autouse=True)
    def frozen_today(self, time_machine):
        time_machine.move_to(datetime(2026, 3, 20, 12, 0, tzinfo=timezone.get_default_timezone()))

    def test_not_overdue_on_due_date(self):
        loan = LoanFactory(loaned_on=TODAY - timedelta(days=14), due_date=TODAY)
        assert not loan.is_overdue
        assert loan.days_overdue() == 0
        assert loan.late_fee == Decimal("0")

    def test_overdue_day_after_due_date(self):
        loan = LoanFactory(loaned_on=TODAY - timedelta(days=20), due_date=TODAY - timedelta(days=3))
        assert loan.is_overdue
        assert loan.days_overdue() == 3
        assert loan.late_fee == Decimal("1.50")

    def test_returned_late_keeps_fee_but_is_not_overdue(self):
        loan = LoanFactory(
            loaned_on=TODAY - timedelta(days=30),
            due_date=TODAY - timedelta(days=10),
            status=Loan.Status.RETURNED,
            returned_at=timezone.now() - timedelta(days=6),
        )
        assert not loan.is_overdue
        assert loan.days_overdue() == 4
        assert loan.late_fee == Decimal("2.00")

    def test_default_due_date_is_14_days(self):
        loan = Loan()
        assert loan.loaned_on == TODAY
        assert loan.due_date == TODAY + timedelta(days=14)

    def test_overdue_queryset(self):
        late = LoanFactory(loaned_on=TODAY - timedelta(days=20), due_date=TODAY - timedelta(days=1))
        LoanFactory(loaned_on=TODAY, due_date=TODAY + timedelta(days=14))
        assert list(Loan.objects.overdue()) == [late]


class TestDatabaseConstraints:
    """These hold even if someone bypasses the services (shell, raw bulk insert)."""

    def test_one_active_loan_per_book_and_member(self):
        loan = LoanFactory(book=BookFactory(copies_total=3))
        with pytest.raises(IntegrityError), transaction.atomic():
            LoanFactory(book=loan.book, member=loan.member)

    def test_same_book_again_after_return_is_allowed(self):
        loan = LoanFactory(status=Loan.Status.RETURNED, returned_at=timezone.now())
        LoanFactory(book=loan.book, member=loan.member)

    def test_due_date_must_be_after_loan_date(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            LoanFactory(loaned_on=TODAY, due_date=TODAY)

    def test_returned_status_requires_returned_at(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            LoanFactory(status=Loan.Status.RETURNED, returned_at=None)

    def test_active_status_forbids_returned_at(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            LoanFactory(status=Loan.Status.ACTIVE, returned_at=timezone.now())

    def test_member_email_is_unique_ignoring_case(self):
        MemberFactory(email="Ann@Example.com")
        with pytest.raises(IntegrityError), transaction.atomic():
            MemberFactory(email="ann@example.com")


class TestBook:
    def test_isbn_is_normalized_on_save(self):
        book = BookFactory(isbn="978-0-13-468599-1")
        book.refresh_from_db()
        assert book.isbn == "9780134685991"

    def test_availability_property_and_annotation_agree(self):
        book = BookFactory(copies_total=3)
        LoanFactory(book=book)
        LoanFactory(book=book, status=Loan.Status.RETURNED, returned_at=timezone.now())
        annotated = Book.objects.with_availability().get(pk=book.pk)
        assert book.copies_available == annotated.copies_available == 2
        assert annotated.copies_on_loan == 1

    def test_cannot_reduce_copies_below_those_on_loan(self):
        book = BookFactory(copies_total=2)
        LoanFactory(book=book)
        LoanFactory(book=book)
        book.copies_total = 1
        with pytest.raises(ValidationError, match="on loan"):
            book.full_clean()


class TestMember:
    def test_loan_counts(self):
        member = MemberFactory()
        LoanFactory(member=member)
        LoanFactory(
            member=member,
            loaned_on=timezone.localdate() - timedelta(days=30),
            due_date=timezone.localdate() - timedelta(days=2),
        )
        LoanFactory(member=member, status=Loan.Status.RETURNED, returned_at=timezone.now())
        annotated = Member.objects.with_loan_counts().get(pk=member.pk)
        assert member.active_loan_count == annotated.active_loan_count == 2
        assert member.overdue_loan_count == annotated.overdue_loan_count == 1

    def test_duplicate_email_is_a_form_error_not_a_crash(self):
        MemberFactory(email="ann@example.com")
        member = Member(full_name="Ann Two", email="ANN@example.com")
        with pytest.raises(ValidationError) as exc:
            member.full_clean()
        assert "email" in exc.value.message_dict


class TestLoanClean:
    def test_returned_loan_cannot_be_reopened(self):
        loan = LoanFactory(status=Loan.Status.RETURNED, returned_at=timezone.now())
        loan.status = Loan.Status.ACTIVE
        loan.returned_at = None
        with pytest.raises(ValidationError, match="cannot be reopened"):
            loan.clean()

    def test_book_and_member_are_fixed_after_creation(self):
        loan = LoanFactory()
        loan.member = MemberFactory()
        with pytest.raises(ValidationError, match="cannot be changed"):
            loan.clean()

    def test_new_loan_runs_borrowing_rules(self):
        member = MemberFactory(status=Member.Status.SUSPENDED)
        loan = Loan(member=member, book=BookFactory())
        with pytest.raises(ValidationError, match="suspended"):
            loan.clean()
