import threading
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from library import services
from library.models import Loan, Member
from library.services import LoanRuleError

from .factories import BookFactory, LoanFactory, MemberFactory

pytestmark = pytest.mark.django_db


class TestCheckout:
    def test_checkout_with_free_copy(self):
        book, member = BookFactory(copies_total=1), MemberFactory()
        loan = services.checkout_book(member=member, book=book)
        assert loan.status == Loan.Status.ACTIVE
        assert loan.due_date == timezone.localdate() + timedelta(days=14)
        assert book.copies_available == 0

    def test_no_free_copy(self):
        book = BookFactory(copies_total=1)
        services.checkout_book(member=MemberFactory(), book=book)
        with pytest.raises(LoanRuleError, match="All 1 copies"):
            services.checkout_book(member=MemberFactory(), book=book)

    def test_suspended_member_cannot_borrow(self):
        member = MemberFactory(status=Member.Status.SUSPENDED)
        with pytest.raises(LoanRuleError, match="suspended"):
            services.checkout_book(member=member, book=BookFactory())

    def test_withdrawn_book_cannot_be_borrowed(self):
        with pytest.raises(LoanRuleError, match="withdrawn"):
            services.checkout_book(member=MemberFactory(), book=BookFactory(is_withdrawn=True))

    @override_settings(MAX_ACTIVE_LOANS=5)
    def test_member_loan_cap(self):
        member = MemberFactory()
        for _ in range(5):
            services.checkout_book(member=member, book=BookFactory())
        with pytest.raises(LoanRuleError, match="already has 5 books"):
            services.checkout_book(member=member, book=BookFactory())

    def test_returned_loans_do_not_count_toward_cap(self):
        member = MemberFactory()
        loan = services.checkout_book(member=member, book=BookFactory())
        services.return_loan(loan)
        assert member.active_loan_count == 0

    def test_same_book_twice_is_rejected(self):
        book, member = BookFactory(copies_total=5), MemberFactory()
        services.checkout_book(member=member, book=book)
        with pytest.raises(LoanRuleError, match="already has a copy"):
            services.checkout_book(member=member, book=book)

    def test_reports_every_broken_rule_at_once(self):
        member = MemberFactory(status=Member.Status.SUSPENDED)
        book = BookFactory(is_withdrawn=True)
        with pytest.raises(LoanRuleError) as exc:
            services.checkout_book(member=member, book=book)
        assert len(exc.value.messages) == 2


class TestReturn:
    def test_return_sets_status_and_timestamp(self):
        loan = LoanFactory()
        services.return_loan(loan)
        loan.refresh_from_db()
        assert loan.status == Loan.Status.RETURNED
        assert loan.returned_at is not None

    def test_double_return_is_rejected(self):
        loan = LoanFactory()
        services.return_loan(loan)
        with pytest.raises(LoanRuleError, match="already returned"):
            services.return_loan(loan)

    def test_return_frees_the_copy(self):
        book = BookFactory(copies_total=1)
        loan = services.checkout_book(member=MemberFactory(), book=book)
        services.return_loan(loan)
        services.checkout_book(member=MemberFactory(), book=book)


class TestExtend:
    today = date(2026, 3, 20)

    def make_loan(self, due_in_days):
        return LoanFactory(
            loaned_on=self.today - timedelta(days=10),
            due_date=self.today + timedelta(days=due_in_days),
        )

    def test_extend_once(self):
        loan = self.make_loan(due_in_days=4)
        services.extend_loan(loan, days=7, today=self.today)
        loan.refresh_from_db()
        assert loan.due_date == self.today + timedelta(days=11)
        assert loan.extended

    def test_second_extension_rejected(self):
        loan = self.make_loan(due_in_days=4)
        services.extend_loan(loan, today=self.today)
        with pytest.raises(LoanRuleError, match="already extended"):
            services.extend_loan(loan, today=self.today)

    @pytest.mark.parametrize("days", [0, 8, -1])
    def test_extension_length_limited(self, days):
        with pytest.raises(LoanRuleError, match="between 1 and 7"):
            services.extend_loan(self.make_loan(due_in_days=4), days=days, today=self.today)

    def test_overdue_loan_cannot_be_extended(self):
        with pytest.raises(LoanRuleError, match="overdue"):
            services.extend_loan(self.make_loan(due_in_days=-1), today=self.today)

    def test_returned_loan_cannot_be_extended(self):
        loan = self.make_loan(due_in_days=4)
        services.return_loan(loan)
        with pytest.raises(LoanRuleError, match="only open loans"):
            services.extend_loan(loan, today=self.today)


def test_bulk_apply_reports_partial_failure():
    loans = [LoanFactory() for _ in range(3)]
    services.return_loan(loans[1])
    result = services.apply_to_each(Loan.objects.order_by("pk"), services.return_loan)
    assert result.succeeded == 2
    assert len(result.failures) == 1
    assert Loan.objects.filter(status=Loan.Status.RETURNED).count() == 3


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_checkouts_of_last_copy_only_one_wins():
    """Two threads (two DB connections) race for the last copy. The row lock
    makes the second wait and then fail cleanly."""
    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL")
    book = BookFactory(copies_total=1)
    members = [MemberFactory(), MemberFactory()]
    barrier = threading.Barrier(2)
    outcomes = []

    def borrow(member):
        try:
            barrier.wait()
            services.checkout_book(member=member, book=book)
            outcomes.append("ok")
        except LoanRuleError:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=borrow, args=(m,)) for m in members]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["ok", "rejected"]
    assert Loan.objects.filter(book=book, status=Loan.Status.ACTIVE).count() == 1
