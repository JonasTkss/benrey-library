"""Library statistics for the Ask AI "statistics" lookup.

Every figure is an aggregate query computed here, on the server. The model
only chooses which METRIC (and optionally which PERIOD) the user asked about;
it never sees the numbers or the rows behind them.
"""

from datetime import timedelta

from django.db.models import (
    Avg,
    Count,
    DateField,
    DurationField,
    ExpressionWrapper,
    F,
    Q,
    Sum,
    Value,
)
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from library.models import LATE_FEE_PER_DAY, Author, Book, Genre, Loan, Member

PERIODS = {
    "today": "today",
    "week": "in the last 7 days",
    "month": "in the last 30 days",
    "year": "in the last 365 days",
    "all": "in total",
}
PERIOD_DAYS = {"today": 0, "week": 6, "month": 29, "year": 364}


def _since(period):
    if period == "all":
        return None
    return timezone.localdate() - timedelta(days=PERIOD_DAYS[period])


def _changelist(model, query=""):
    return reverse(f"admin:library_{model}_changelist") + query


def fees_due(period, row):
    """Late fees on books that are still out. Payments are not recorded, so
    fees on books already returned cannot be called paid or unpaid."""
    today = timezone.localdate()
    overdue = Loan.objects.overdue(today)
    days_late = overdue.aggregate(
        days=Sum(
            ExpressionWrapper(
                Value(today, output_field=DateField()) - F("due_date"),
                output_field=DurationField(),
            )
        )
    )["days"]
    total_days = days_late.days if days_late else 0
    total = LATE_FEE_PER_DAY * total_days
    owing = overdue.values("member").distinct().count()
    average = total / owing if owing else 0
    return (
        f"€{total:.2f} in late fees is due right now, owed by {owing} members "
        f"(about €{average:.2f} each). This counts books that are still out; payments are not "
        "recorded, so fees on books already returned are not included.",
        [
            row("Fees due now", f"€{total:.2f}", _changelist("member", "?overdue=yes")),
            row("Members owing", str(owing), _changelist("member", "?overdue=yes")),
            row("Overdue loans", str(overdue.count()), _changelist("loan", "?due=overdue")),
        ],
    )


def overdue(period, row):
    today = timezone.localdate()
    loans = Loan.objects.overdue(today)
    count = loans.count()
    members = loans.values("member").distinct().count()
    oldest = loans.order_by("due_date").values_list("due_date", flat=True).first()
    active = Loan.objects.active().count()
    share = round(100 * count / active) if active else 0
    oldest_days = (today - oldest).days if oldest else 0
    return (
        f"{count} loans are overdue ({share}% of the {active} books out), held by {members} "
        f"members. The oldest is {oldest_days} days late.",
        [
            row("Overdue loans", str(count), _changelist("loan", "?due=overdue")),
            row("Members with overdue books", str(members), _changelist("member", "?overdue=yes")),
            row("Oldest", f"{oldest_days} days late", _changelist("loan", "?due=overdue")),
        ],
    )


def members(period, row):
    figures = Member.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=Member.Status.ACTIVE)),
        suspended=Count("id", filter=Q(status=Member.Status.SUSPENDED)),
    )
    since = _since(period)
    joined = Member.objects.filter(joined_at__gte=since).count() if since else figures["total"]
    borrowing = Loan.objects.active().values("member").distinct().count()
    return (
        f"{figures['total']} members: {figures['active']} active, {figures['suspended']} "
        f"suspended. {borrowing} have a book out right now; {joined} joined {PERIODS[period]}.",
        [
            row("Active", str(figures["active"]), _changelist("member", "?status__exact=active")),
            row(
                "Suspended",
                str(figures["suspended"]),
                _changelist("member", "?status__exact=suspended"),
            ),
            row(f"Joined {PERIODS[period]}", str(joined), _changelist("member")),
        ],
    )


def catalogue(period, row):
    figures = Book.objects.aggregate(
        titles=Count("id"),
        copies=Sum("copies_total"),
        withdrawn=Count("id", filter=Q(is_withdrawn=True)),
    )
    copies = figures["copies"] or 0
    out = Loan.objects.active().count()
    authors = Author.objects.count()
    on_shelf = copies - out
    share = round(100 * out / copies) if copies else 0
    return (
        f"{figures['titles']} titles by {authors} authors, {copies} copies in total: "
        f"{on_shelf} on the shelf and {out} on loan ({share}%). {figures['withdrawn']} titles "
        "are withdrawn.",
        [
            row("Titles", str(figures["titles"]), _changelist("book")),
            row(
                "Copies on the shelf", str(on_shelf), _changelist("book", "?availability=available")
            ),
            row("Copies on loan", str(out), _changelist("loan", "?status__exact=active")),
            row("Authors", str(authors), _changelist("author")),
        ],
    )


def loans_activity(period, row):
    since = _since(period)
    lent = Loan.objects.all()
    returned = Loan.objects.filter(status=Loan.Status.RETURNED)
    if since:
        lent = lent.filter(loaned_on__gte=since)
        returned = returned.filter(returned_at__date__gte=since)
    lent_n, returned_n = lent.count(), returned.count()
    busiest = (
        lent.values("loaned_on").annotate(n=Count("id")).order_by("-n", "-loaned_on").first()
        if since
        else None
    )
    summary = f"{lent_n} books were lent and {returned_n} returned {PERIODS[period]}."
    if busiest and period != "today":
        summary += f" Busiest day: {busiest['loaned_on']:%d %b} with {busiest['n']} loans."
    return summary, [
        row(f"Lent {PERIODS[period]}", str(lent_n), _changelist("loan")),
        row(
            f"Returned {PERIODS[period]}",
            str(returned_n),
            _changelist("loan", "?status__exact=returned"),
        ),
    ]


def loan_length(period, row):
    since = _since(period)
    returned = Loan.objects.filter(status=Loan.Status.RETURNED)
    if since:
        returned = returned.filter(returned_at__date__gte=since)
    returned = returned.annotate(day=TruncDate("returned_at"))
    figures = returned.aggregate(
        n=Count("id"),
        late=Count("id", filter=Q(day__gt=F("due_date"))),
        avg=Avg(ExpressionWrapper(F("day") - F("loaned_on"), output_field=DurationField())),
    )
    n = figures["n"]
    if not n:
        return f"No books were returned {PERIODS[period]}.", []
    avg_days = figures["avg"].total_seconds() / 86400 if figures["avg"] else 0
    late_share = round(100 * figures["late"] / n)
    return (
        f"Books returned {PERIODS[period]} were kept {avg_days:.1f} days on average. "
        f"{late_share}% came back late ({figures['late']} of {n}).",
        [
            row(
                "Average days kept",
                f"{avg_days:.1f}",
                _changelist("loan", "?status__exact=returned"),
            ),
            row("Returned late", f"{late_share}%", _changelist("loan", "?due=returned_late")),
        ],
    )


def genres(period, row):
    since = _since(period)
    loans = Loan.objects.all()
    if since:
        loans = loans.filter(loaned_on__gte=since)
    by_genre = list(loans.values("book__genre").annotate(n=Count("id")).order_by("-n")[:5])
    if not by_genre:
        return f"No books were lent {PERIODS[period]}.", []
    labels = dict(Genre.choices)
    top = by_genre[0]
    return (
        f"{labels.get(top['book__genre'], top['book__genre'])} is the most borrowed genre "
        f"{PERIODS[period]} with {top['n']} loans.",
        [
            row(
                labels.get(g["book__genre"], g["book__genre"]),
                f"{g['n']} loans",
                _changelist("book", f"?genre__exact={g['book__genre']}"),
            )
            for g in by_genre
        ],
    )


def overview(period, row):
    today = timezone.localdate()
    out = Loan.objects.active().count()
    late = Loan.objects.overdue(today).count()
    copies = Book.objects.aggregate(n=Sum("copies_total"))["n"] or 0
    active_members = Member.objects.filter(status=Member.Status.ACTIVE).count()
    fee_line, fee_rows = fees_due(period, row)
    month_lent = Loan.objects.filter(loaned_on__gte=today - timedelta(days=29)).count()
    summary = (
        f"{out} of {copies} copies are on loan, {late} of them overdue, with "
        f"{fee_rows[0].detail} in late fees due. {active_members} active members; "
        f"{month_lent} books lent in the last 30 days."
    )
    rows = [
        row("On loan now", str(out), _changelist("loan", "?status__exact=active")),
        row("Overdue", str(late), _changelist("loan", "?due=overdue")),
        fee_rows[0],
        row("Active members", str(active_members), _changelist("member", "?status__exact=active")),
        row("Lent in the last 30 days", str(month_lent), _changelist("loan")),
    ]
    return summary, rows


METRICS = {
    "overview": ("Key figures at a glance.", overview),
    "fees_due": ("Total late fees due now and how many members owe them.", fees_due),
    "overdue": ("How many loans are overdue, share of loans out, oldest.", overdue),
    "members": ("Member totals: active, suspended, borrowing now, new joiners.", members),
    "catalogue": ("Titles, copies, on shelf vs on loan, authors, withdrawn.", catalogue),
    "loans_activity": ("How many books were lent and returned in a period.", loans_activity),
    "loan_length": ("Average days a book is kept, share returned late.", loan_length),
    "genres": ("Most borrowed genres in a period.", genres),
}
