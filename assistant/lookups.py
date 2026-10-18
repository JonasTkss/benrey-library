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

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from library.models import Author, Book, Loan, Member

from . import stats

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
    params: dict = field(default_factory=dict)  # the validated params that were used


@dataclass(frozen=True)
class Lookup:
    name: str
    description: str  # shown to the model so it can pick the right one
    params: dict[str, str]  # parameter name -> description (all optional or strings)
    permission: str
    run: Callable[..., LookupResult]
    choices: dict[str, dict[str, str]] = field(default_factory=dict)  # enum params
    defaults: dict[str, str] = field(default_factory=dict)


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


def _book_row(book):
    state = (
        "withdrawn"
        if book.is_withdrawn
        else f"{book.copies_available} of {book.copies_total} on the shelf"
    )
    return Row(book.title, state, reverse("admin:library_book_change", args=[book.pk]))


def top_authors(limit=DEFAULT_LIMIT):
    authors = list(
        Author.objects.annotate(n=Count("books")).filter(n__gt=0).order_by("-n", "name")[:limit]
    )
    if not authors:
        return LookupResult("There are no books in the catalogue yet.")
    top = authors[0]
    return LookupResult(
        f"{top.name} has the most books in the catalogue: {top.n}.",
        [
            Row(a.name, f"{a.n} book(s)", reverse("admin:library_author_change", args=[a.pk]))
            for a in authors
        ],
    )


def author_books(query):
    authors = list(Author.objects.filter(name__icontains=query).order_by("name")[:MAX_LIMIT])
    if not authors:
        return LookupResult(f"No author matches “{query}”.")
    if len(authors) > 1:
        return LookupResult(
            f"{len(authors)} authors match “{query}”. Which one?",
            [
                Row(a.name, "author", reverse("admin:library_author_change", args=[a.pk]))
                for a in authors
            ],
        )
    author = authors[0]
    all_books = Book.objects.with_availability().filter(authors=author).order_by("title")
    total = all_books.count()
    books = list(all_books[:MAX_LIMIT])
    summary = f"{author.name} has {total} book(s) in the catalogue."
    if total > len(books):
        summary += f" The first {len(books)} are below; the author page lists them all."
    rows = [_book_row(b) for b in books]
    if total > len(books):
        author_url = reverse("admin:library_author_change", args=[author.pk])
        rows.append(Row(f"All books by {author.name}", "author page", author_url))
    return LookupResult(summary, rows)


def top_borrowers(limit=DEFAULT_LIMIT):
    members = list(
        Member.objects.with_loan_counts()
        .filter(annotated_active_loans__gt=0)
        .order_by("-annotated_active_loans", "full_name")[:limit]
    )
    if not members:
        return LookupResult("Nobody has a book on loan right now.")
    top = members[0]
    return LookupResult(
        f"{top.full_name} has the most books on loan right now: {top.active_loan_count}.",
        [_member_row(m) for m in members],
    )


def most_loans_ever(limit=DEFAULT_LIMIT):
    members = list(
        Member.objects.with_loan_counts()
        .annotate(total_loans=Count("loans", distinct=True))
        .filter(total_loans__gt=0)
        .order_by("-total_loans", "full_name")[:limit]
    )
    if not members:
        return LookupResult("No loans have been recorded yet.")
    rows = []
    for m in members:
        row = _member_row(m)
        row.detail = f"{m.total_loans} loans in total · " + row.detail
        rows.append(row)
    top = members[0]
    return LookupResult(
        f"{top.full_name} has borrowed the most books overall: {top.total_loans} loans.", rows
    )


def popular_books(limit=DEFAULT_LIMIT):
    books = list(
        Book.objects.with_availability()
        .annotate(times_lent=Count("loans", distinct=True))
        .filter(times_lent__gt=0)
        .order_by("-times_lent", "title")[:limit]
    )
    if not books:
        return LookupResult("No book has been lent yet.")
    rows = []
    for b in books:
        row = _book_row(b)
        row.detail = f"lent {b.times_lent} times · " + row.detail
        rows.append(row)
    return LookupResult(
        f"“{books[0].title}” is the most borrowed book: lent {books[0].times_lent} times.", rows
    )


def member_loans(query):
    members = list(
        Member.objects.filter(
            Q(card_number__iexact=query) | Q(email__iexact=query) | Q(full_name__icontains=query)
        ).order_by("full_name")[:2]
    )
    if not members:
        return LookupResult(f"No member matches “{query}”.")
    if len(members) > 1:
        return find_member(query)  # ambiguous: list the matches instead
    member = members[0]
    loans = list(member.loans.active().select_related("book").order_by("due_date"))
    if not loans:
        return LookupResult(f"{member.full_name} has no books on loan.")
    rows = [
        Row(
            loan.book.title,
            f"due {loan.due_date:%d %b}"
            + (f" · {loan.days_overdue()} days late" if loan.is_overdue else ""),
            reverse("admin:library_loan_change", args=[loan.pk]),
        )
        for loan in loans
    ]
    return LookupResult(f"{member.full_name} has {len(loans)} book(s) on loan:", rows)


def statistics(metric, period):
    summary, rows = stats.METRICS[metric][1](period, Row)
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
            "member_loans",
            "Which books a specific member has on loan right now, with due dates.",
            {"query": "the member's name, email or card number (required)"},
            "library.view_loan",
            member_loans,
        ),
        Lookup(
            "top_borrowers",
            "Members with the most books on loan right now.",
            {"limit": f"how many members to list, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT})"},
            "library.view_member",
            top_borrowers,
        ),
        Lookup(
            "most_loans_ever",
            "Members who have borrowed the most books in total (all time).",
            {"limit": f"how many members to list, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT})"},
            "library.view_member",
            most_loans_ever,
        ),
        Lookup(
            "popular_books",
            "The most borrowed books (all time).",
            {"limit": f"how many books to list, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT})"},
            "library.view_book",
            popular_books,
        ),
        Lookup(
            "top_authors",
            "Authors with the most books in the catalogue.",
            {"limit": f"how many authors to list, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT})"},
            "library.view_author",
            top_authors,
        ),
        Lookup(
            "author_books",
            "The books by a specific author, with availability.",
            {"query": "the author's name (required)"},
            "library.view_book",
            author_books,
        ),
        Lookup(
            "statistics",
            "Library statistics and totals (sums, counts, averages, shares). Pick a metric; "
            "loans_activity, loan_length, genres and members also take a period.",
            {
                "metric": "one of: "
                + "; ".join(f"{k} = {desc}" for k, (desc, _) in stats.METRICS.items()),
                "period": "one of: " + ", ".join(stats.PERIODS) + " (default all)",
            },
            "library.view_loan",
            statistics,
            choices={"metric": dict.fromkeys(stats.METRICS, ""), "period": stats.PERIODS},
            defaults={"metric": "overview", "period": "all"},
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
    for name_, allowed in lookup.choices.items():
        value = params.get(name_, lookup.defaults.get(name_))
        if value not in allowed:
            value = lookup.defaults.get(name_)  # unknown choice: fall back, never pass through
        kwargs[name_] = value
    if "query" in lookup.params:
        query = params.get("query")
        if not isinstance(query, str) or not MIN_QUERY <= len(query.strip()) <= MAX_QUERY:
            raise LookupRejected("a search term of 2-100 characters is needed")
        kwargs["query"] = query.strip()
    result = lookup.run(**kwargs)
    result.params = kwargs
    return result


# Keyword routing for when the AI is unavailable. Deliberately narrow: only
# clear "who/which/how many" data questions; everything else gets field help.
ASKING = {"who", "which", "whose", "list", "show", "top", "highest", "most", "biggest", "many"}
RANKING = {"most", "highest", "top", "biggest", "largest"}
FEES = {"fee", "fees", "bill", "bills", "owe", "owes", "owing", "debt", "pay"}
LOANS = {"loan", "loans", "borrowed", "borrows", "borrowing", "books"}


# "totals" (plural, e.g. "today's totals") is left to circulation_summary.
STATS = {
    "total",
    "sum",
    "average",
    "avg",
    "percent",
    "percentage",
    "share",
    "statistics",
    "stats",
    "rate",
    "much",
}
PERIOD_WORDS = {"today": "today", "week": "week", "month": "month", "year": "year"}


def _stats_metric(words: set[str]) -> str:
    if words & FEES or "unpaid" in words:
        return "fees_due"
    if words & {"overdue", "late"} and not words & {"kept", "long"}:
        return "overdue"
    if words & {"genre", "genres"}:
        return "genres"
    if words & {"kept", "long", "length", "late"}:
        return "loan_length"
    if words & {"member", "members", "suspended"}:
        return "members"
    if words & {"lent", "borrowed", "returned", "returns"}:
        return "loans_activity"
    if words & {"book", "books", "copies", "catalogue", "titles", "shelf"}:
        return "catalogue"
    return "overview"


def guess(question: str) -> tuple[str, dict] | None:
    """(lookup name, params) for clear data questions, else None."""
    words = set(re.findall(r"[a-z]+", question.lower()))
    if words & STATS:
        period = next((p for w, p in PERIOD_WORDS.items() if w in words), "all")
        return "statistics", {"metric": _stats_metric(words), "period": period}
    if not words & ASKING:
        return None
    if words & FEES:
        return "top_fees", {}
    if words & {"author", "authors", "writer", "writers", "wrote"} and words & RANKING:
        return "top_authors", {}
    if words & {"overdue", "late"}:
        return "overdue_loans", {}
    if words & {"popular", "read"} or ({"book", "books"} & words and "borrowed" in words):
        return "popular_books", {}
    if words & LOANS and words & RANKING:
        return "top_borrowers", {}
    if words & {"today", "summary", "totals", "loan", "loans"}:
        return "circulation_summary", {}
    return None
