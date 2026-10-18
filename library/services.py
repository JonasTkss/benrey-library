"""Circulation operations: checkout, return, extend.

Every state change to a loan goes through here, from the admin form, the
inline, and the bulk actions, so the rules are written once.

Concurrency: checkout locks the book row and then the member row with
SELECT ... FOR UPDATE inside a transaction. Two librarians lending the last
copy at the same moment are serialized: the second one waits, then sees zero
free copies and gets a clear error. Locks are always taken in the same order
(book, then member) so two checkouts cannot deadlock each other.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import LOAN_PERIOD_DAYS, MAX_EXTENSION_DAYS, Book, Loan, Member


class LoanRuleError(ValidationError):
    """A circulation rule was broken. Subclasses ValidationError so admin
    forms show it as a normal form error."""


def checkout_book(
    *,
    member: Member,
    book: Book,
    loaned_on: date | None = None,
    due_date: date | None = None,
    notes: str = "",
) -> Loan:
    loaned_on = loaned_on or timezone.localdate()
    due_date = due_date or loaned_on + timedelta(days=LOAN_PERIOD_DAYS)
    with transaction.atomic():
        book = Book.objects.select_for_update().get(pk=book.pk)
        member = Member.objects.select_for_update().get(pk=member.pk)
        problems = Loan.borrowing_rule_violations(member, book)
        if problems:
            raise LoanRuleError(problems)
        loan = Loan(member=member, book=book, loaned_on=loaned_on, due_date=due_date, notes=notes)
        # validate_constraints=False: the partial unique constraint was checked
        # above under the lock, and the DB enforces it again on insert.
        loan.full_clean(validate_constraints=False)
        loan.save()
    return loan


def return_loan(loan: Loan, *, when: datetime | None = None) -> Loan:
    """ACTIVE -> RETURNED. RETURNED is terminal, so returning twice is an error."""
    with transaction.atomic():
        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        if loan.status == Loan.Status.RETURNED:
            raise LoanRuleError(f"'{loan}' was already returned.")
        loan.status = Loan.Status.RETURNED
        loan.returned_at = when or timezone.now()
        loan.save(update_fields=["status", "returned_at"])
    return loan


def extend_loan(loan: Loan, *, days: int = MAX_EXTENSION_DAYS, today: date | None = None) -> Loan:
    """Push the due date back once, by 1-7 days, while the loan is not overdue."""
    if not 1 <= days <= MAX_EXTENSION_DAYS:
        raise LoanRuleError(f"An extension must be between 1 and {MAX_EXTENSION_DAYS} days.")
    today = today or timezone.localdate()
    with transaction.atomic():
        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        if loan.status != Loan.Status.ACTIVE:
            raise LoanRuleError(f"'{loan}' is returned; only open loans can be extended.")
        if loan.extended:
            raise LoanRuleError(f"'{loan}' was already extended once.")
        if loan.days_overdue(today) > 0:
            raise LoanRuleError(f"'{loan}' is overdue; return it instead of extending.")
        loan.due_date += timedelta(days=days)
        loan.extended = True
        loan.save(update_fields=["due_date", "extended"])
    return loan


@dataclass
class BulkResult:
    succeeded: int = 0
    failures: list[str] = field(default_factory=list)


def apply_to_each(loans, operation, **kwargs) -> BulkResult:
    """Run a service on many loans, each in its own transaction.

    Partial success is intended: one already-returned loan in a selection of
    500 should not roll back the other 499. Every failure is reported back.
    """
    result = BulkResult()
    for loan in loans:
        try:
            operation(loan, **kwargs)
        except LoanRuleError as exc:
            result.failures.extend(exc.messages)
        else:
            result.succeeded += 1
    return result
