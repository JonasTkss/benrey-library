"""Read-only data lookups the assistant can answer with.

The AI model never sees library data. For a question like "who has the
highest bill?" it only picks a lookup by name from this list (and maybe a
search term). The server then checks the user's permission, runs the query
here, and builds the answer and links itself. So:
- no member names, emails or fees are sent to the AI provider,
- numbers cannot be hallucinated: they come straight from the database,
- the assistant can only show what the user could open in the admin anyway,
- the model cannot run anything that is not in LOOKUPS.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from library.models import Book, Loan, Member

DEFAULT_LIMIT, MAX_LIMIT = 5, 10
MIN_QUERY, MAX_QUERY = 2, 100


class LookupRejected(Exception):
    """The requested lookup is unknown, not allowed, or has bad parameters."""


@dataclass
class Row:
    label: str
    detail: str
    url: str


@dataclass
class LookupResult:
    summary: str
    rows: list[Row] = field(default_factory=list)


@dataclass(frozen=True)
class Lookup:
    name: str
    description: str  # shown to the model so it can pick the right one
    params: dict[str, str]  # parameter name -> description (all optional or strings)
    permission: str
    run: Callable[..., LookupResult]


def _member_url(member):
    return reverse("admin:library_member_change", args=[member.pk])


def _member_row(member):
    parts = [member.card_number, f"{member.active_loan_count} on loan"]
    if member.overdue_loan_count:
        parts.append(f"{member.overdue_loan_count} overdue")
    parts.append(f"€{member.fees_due:.2f} due")
    if member.status == Member.Status.SUSPENDED:
        parts.append("suspended")
    return Row(member.full_name, " · ".join(parts), _member_url(member))


def top_fees(limit=DEFAULT_LIMIT):
    members = list(Member.objects.by_fees_due()[:limit])
    if not members:
        return LookupResult("Nobody owes late fees right now.")
    top = members[0]
    summary = (
        f"{top.full_name} has the highest bill: €{top.fees_due:.2f} in late fees "
        f"on {top.overdue_loan_count} overdue book(s)."
    )
    if len(members) > 1:
        summary += f" Here are the top {len(members)}."
    return LookupResult(summary, [_member_row(m) for m in members])


def overdue_loans(limit=DEFAULT_LIMIT):
    loans = list(
        Loan.objects.overdue().select_related("book", "member").order_by("due_date", "id")[:limit]
    )
    total = Loan.objects.overdue().count()
    if not loans:
        return LookupResult("No loans are overdue right now.")
    rows = [
        Row(
            f"{loan.book.title} → {loan.member.full_name}",
            f"due {loan.due_date:%d %b}, {loan.days_overdue()} days late, €{loan.late_fee:.2f}",
            reverse("admin:library_loan_change", args=[loan.pk]),
        )
        for loan in loans
    ]
    return LookupResult(f"{total} loans are overdue. The oldest {len(rows)}:", rows)


def find_member(query):
    members = list(
        Member.objects.with_loan_counts()
        .filter(
            Q(card_number__iexact=query) | Q(email__iexact=query) | Q(full_name__icontains=query)
        )
        .order_by("full_name")[:MAX_LIMIT]
    )
    if not members:
        return LookupResult(f"No member matches “{query}”.")
    if len(members) == 1:
        m = members[0]
        summary = (
            f"{m.full_name} ({m.card_number}) has {m.active_loan_count} book(s) on loan, "
            f"{m.overdue_loan_count} overdue, and owes €{m.fees_due:.2f}."
        )
    else:
        summary = f"{len(members)} members match “{query}”:"
    return LookupResult(summary, [_member_row(m) for m in members])


def book_availability(query):
    books = list(
        Book.objects.with_availability()
        .filter(Q(title__icontains=query) | Q(isbn=query.replace("-", "").replace(" ", "")))
        .order_by("title")[:MAX_LIMIT]
    )
    if not books:
        return LookupResult(f"No book matches “{query}”.")
    rows = []
    for book in books:
        state = (
            "withdrawn"
            if book.is_withdrawn
            else f"{book.copies_available} of {book.copies_total} on the shelf"
        )
        rows.append(Row(book.title, state, reverse("admin:library_book_change", args=[book.pk])))
    if len(books) == 1:
        summary = f"“{books[0].title}”: {rows[0].detail}."
    else:
        summary = f"{len(books)} books match “{query}”:"
    return LookupResult(summary, rows)


def circulation_summary():
    today = timezone.localdate()
    active = Loan.objects.active()
    on_loan = active.count()
    overdue = active.filter(due_date__lt=today).count()
    due_soon = active.filter(due_date__gte=today, due_date__lte=today + timedelta(days=3)).count()
    lent_today = Loan.objects.filter(loaned_on=today).count()
    returned_today = Loan.objects.filter(returned_at__date=today).count()
    return LookupResult(
        f"{on_loan} books are on loan, {overdue} of them overdue and {due_soon} due in the next "
        f"3 days. Today {lent_today} were lent and {returned_today} returned.",
        [
            Row(
                "Overdue loans",
                f"{overdue} loans",
                reverse("admin:library_loan_changelist") + "?due=overdue",
            ),
            Row(
                "Due in the next 3 days",
                f"{due_soon} loans",
                reverse("admin:library_loan_changelist") + "?due=soon",
            ),
        ],
    )


LOOKUPS = {
    lookup.name: lookup
    for lookup in [
        Lookup(
            "top_fees",
            "Members who owe the most late fees right now, highest bill first.",
            {"limit": f"how many members to list, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT})"},
            "library.view_member",
            top_fees,
        ),
        Lookup(
            "overdue_loans",
            "Loans past their due date, oldest first, with who has the book.",
            {"limit": f"how many loans to list, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT})"},
            "library.view_loan",
            overdue_loans,
        ),
        Lookup(
            "find_member",
            "Look up a member: card number, books on loan, overdue books and fees due.",
            {"query": "the member's name, email or card number (required)"},
            "library.view_member",
            find_member,
        ),
        Lookup(
            "book_availability",
            "Whether a book is on the shelf: copies available out of total.",
            {"query": "the book title or ISBN (required)"},
            "library.view_book",
            book_availability,
        ),
        Lookup(
            "circulation_summary",
            "Today's totals: books on loan, overdue, due soon, lent and returned today.",
            {},
            "library.view_loan",
            circulation_summary,
        ),
    ]
}


def allowed_lookups(user) -> list[Lookup]:
    return [lookup for lookup in LOOKUPS.values() if user.has_perm(lookup.permission)]


def describe_for_prompt(user) -> str:
    lines = []
    for lookup in allowed_lookups(user):
        params = ", ".join(f"{name}: {desc}" for name, desc in lookup.params.items()) or "none"
        lines.append(f"- {lookup.name}: {lookup.description} Parameters: {params}.")
    return "\n".join(lines) or "(none available to this user)"


def run(user, name, params) -> LookupResult:
    """Validate and run a lookup for this user. Raises LookupRejected if the name
    is unknown, the user lacks the permission, or the parameters are bad."""
    lookup = LOOKUPS.get(name)
    if lookup is None or not user.has_perm(lookup.permission):
        raise LookupRejected(f"lookup {name!r} is not available")
    params = params if isinstance(params, dict) else {}
    kwargs = {}
    if "limit" in lookup.params:
        try:
            limit = int(params.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        kwargs["limit"] = max(1, min(MAX_LIMIT, limit))
    if "query" in lookup.params:
        query = params.get("query")
        if not isinstance(query, str) or not MIN_QUERY <= len(query.strip()) <= MAX_QUERY:
            raise LookupRejected("a search term of 2-100 characters is needed")
        kwargs["query"] = query.strip()
    return lookup.run(**kwargs)


# Keyword routing for when the AI is unavailable. Deliberately narrow: only
# clear "who/which/how many" data questions; everything else gets field help.
ASKING = {"who", "which", "whose", "list", "show", "top", "highest", "most", "biggest", "many"}
ROUTES = [
    ("top_fees", {"fee", "fees", "bill", "bills", "owe", "owes", "owing", "debt", "pay"}),
    ("overdue_loans", {"overdue", "late"}),
    ("circulation_summary", {"today", "summary", "totals", "loan", "loans"}),
]


def guess(question: str) -> str | None:
    words = set(re.findall(r"[a-z]+", question.lower()))
    if not words & ASKING:
        return None
    for name, topic in ROUTES:
        if words & topic:
            return name
    return None
