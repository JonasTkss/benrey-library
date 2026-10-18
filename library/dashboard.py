"""The admin's home page, sidebar menu and guide page.

Everything here is read-only and role-aware: the sidebar hides links a user
has no permission for, and the dashboard only queries and shows the panels
that the user's role can act on. Permissions are still enforced by each admin
view; this module only decides what to *show*.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import TemplateView

from assistant.context import business_rules

from .models import LATE_FEE_PER_DAY, LOAN_PERIOD_DAYS, MAX_EXTENSION_DAYS, Book, Loan, Member

CHART_DAYS = 14


def _perm(codename):
    return lambda request: request.user.has_perm(codename)


def overdue_badge(request):
    """Number shown next to "Loans" in the sidebar."""
    count = Loan.objects.overdue().count()
    return str(count) if count else None


def sidebar_navigation(request):
    url = reverse_lazy
    return [
        {
            "title": "Overview",
            "items": [
                {"title": "Dashboard", "icon": "space_dashboard", "link": url("admin:index")},
                {"title": "How to use", "icon": "menu_book", "link": url("admin:guide")},
            ],
        },
        {
            "title": "Circulation",
            "separator": True,
            "items": [
                {
                    "title": "Loans",
                    "icon": "swap_horiz",
                    "link": url("admin:library_loan_changelist"),
                    "badge": "library.dashboard.overdue_badge",
                    "badge_variant": "danger",
                    "permission": _perm("library.view_loan"),
                },
                {
                    "title": "Overdue loans",
                    "icon": "schedule",
                    "link": lambda request: (
                        reverse("admin:library_loan_changelist") + "?due=overdue"
                    ),
                    "permission": _perm("library.view_loan"),
                },
                {
                    "title": "Members",
                    "icon": "group",
                    "link": url("admin:library_member_changelist"),
                    "permission": _perm("library.view_member"),
                },
            ],
        },
        {
            "title": "Catalogue",
            "separator": True,
            "items": [
                {
                    "title": "Books",
                    "icon": "auto_stories",
                    "link": url("admin:library_book_changelist"),
                    "permission": _perm("library.view_book"),
                },
                {
                    "title": "Authors",
                    "icon": "history_edu",
                    "link": url("admin:library_author_changelist"),
                    "permission": _perm("library.view_author"),
                },
            ],
        },
        {
            "title": "Administration",
            "separator": True,
            "items": [
                {
                    "title": "Staff accounts",
                    "icon": "manage_accounts",
                    "link": url("admin:auth_user_changelist"),
                    "permission": _perm("auth.view_user"),
                },
                {
                    "title": "Roles",
                    "icon": "shield_person",
                    "link": url("admin:auth_group_changelist"),
                    "permission": _perm("auth.view_group"),
                },
                {
                    "title": "Login attempts",
                    "icon": "lock_clock",
                    "link": url("admin:axes_accessattempt_changelist"),
                    "permission": _perm("axes.view_accessattempt"),
                },
            ],
        },
    ]


def _loan_activity_chart(today):
    """Loans out vs. books returned per day for the last two weeks."""
    start = today - timedelta(days=CHART_DAYS - 1)
    lent = dict(
        Loan.objects.filter(loaned_on__gte=start)
        .values("loaned_on")
        .annotate(n=Count("id"))
        .values_list("loaned_on", "n")
    )
    returned = dict(
        Loan.objects.filter(returned_at__date__gte=start)
        .annotate(day=TruncDate("returned_at"))
        .values("day")
        .annotate(n=Count("id"))
        .values_list("day", "n")
    )
    days = [start + timedelta(days=i) for i in range(CHART_DAYS)]
    return json.dumps(
        {
            "labels": [d.strftime("%d %b") for d in days],
            "datasets": [
                {
                    "label": "Lent",
                    "data": [lent.get(d, 0) for d in days],
                    "backgroundColor": "var(--color-primary-600)",
                },
                {
                    "label": "Returned",
                    "data": [returned.get(d, 0) for d in days],
                    "backgroundColor": "var(--color-primary-300)",
                },
            ],
        }
    )


# Passing options replaces Unfold's chart defaults, so the essentials are here.
CHART_OPTIONS = json.dumps(
    {
        "responsive": True,
        "maintainAspectRatio": False,
        "plugins": {"legend": {"display": True, "position": "bottom", "align": "end"}},
        "datasets": {"bar": {"borderRadius": 4, "maxBarThickness": 16, "categoryPercentage": 0.7}},
        "scales": {
            "x": {"grid": {"display": False}, "border": {"display": False}},
            "y": {"beginAtZero": True, "ticks": {"precision": 0}, "border": {"display": False}},
        },
    }
)


def dashboard_callback(request, context):
    """Adds the numbers for templates/admin/index.html."""
    user = request.user
    today = timezone.localdate()
    can_loans = user.has_perm("library.view_loan")
    can_books = user.has_perm("library.view_book")

    context["role_names"] = ", ".join(user.groups.values_list("name", flat=True)) or (
        "Superuser" if user.is_superuser else "No role"
    )
    context["can_add_loan"] = user.has_perm("library.add_loan")
    context["can_add_member"] = user.has_perm("library.add_member")
    context["can_add_book"] = user.has_perm("library.add_book")

    if can_loans:
        active = Loan.objects.active()
        stats = active.aggregate(
            on_loan=Count("id"),
            overdue=Count("id", filter=Q(due_date__lt=today)),
            due_soon=Count("id", filter=Q(due_date__gte=today, due_date__lte=today + timedelta(3))),
            lent_today=Count("id", filter=Q(loaned_on=today)),
        )
        context["loan_stats"] = stats
        context["overdue_loans"] = (
            Loan.objects.overdue().select_related("book", "member").order_by("due_date")[:8]
        )
        context["due_soon_loans"] = (
            active.filter(due_date__gte=today, due_date__lte=today + timedelta(3))
            .select_related("book", "member")
            .order_by("due_date")[:6]
        )
        context["activity_chart"] = _loan_activity_chart(today)
        context["activity_chart_options"] = CHART_OPTIONS
        context["returned_today"] = Loan.objects.filter(returned_at__date=today).count()

    if user.has_perm("library.view_member"):
        context["member_stats"] = Member.objects.aggregate(
            active=Count("id", filter=Q(status=Member.Status.ACTIVE)),
            suspended=Count("id", filter=Q(status=Member.Status.SUSPENDED)),
        )

    if can_books:
        totals = Book.objects.aggregate(
            titles=Count("id"),
            copies=Sum("copies_total"),
            withdrawn=Count("id", filter=Q(is_withdrawn=True)),
        )
        totals["copies"] = totals["copies"] or 0
        totals["on_shelf"] = totals["copies"] - Loan.objects.active().count()
        context["book_stats"] = totals

    if user.has_perm("axes.view_accessattempt"):
        from axes.models import AccessAttempt

        context["show_security"] = True
        context["failed_logins"] = AccessAttempt.objects.filter(
            attempt_time__gte=timezone.now() - timedelta(days=1)
        ).count()

    return context


class GuideView(TemplateView):
    """Plain-language manual inside the admin, written for the person reading it."""

    template_name = "admin/guide.html"
    admin_site = None  # set by Unfold when it registers SITE_VIEWS

    def get_context_data(self, **kwargs):
        user = self.request.user
        return {
            **super().get_context_data(**kwargs),
            **self.admin_site.each_context(self.request),
            "title": "How to use Benrey Library",
            "is_admin_role": user.has_perm("library.change_book"),
            "can_change_status": user.has_perm("library.change_member_status"),
            "can_manage_users": user.has_perm("auth.change_user"),
            "max_loans": settings.MAX_ACTIVE_LOANS,
            "loan_days": LOAN_PERIOD_DAYS,
            "extension_days": MAX_EXTENSION_DAYS,
            "late_fee": LATE_FEE_PER_DAY,
            "rules": business_rules().splitlines(),
        }
