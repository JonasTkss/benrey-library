"""Admin configuration: the whole staff UI.

Role behaviour comes from two layers:
1. Django permissions (granted per group in migrations/0002_role_groups.py).
   The admin enforces these itself: no add permission = no Add button and a
   403 on the add URL, no delete permission = no delete action or button,
   view-only = read-only form.
2. The overrides below, which change what each role *sees*: extra columns
   and actions for catalogue managers, a locked status field for librarians.
Both are checked on the server; nothing is hidden with CSS.
"""

from datetime import timedelta

from axes.admin import AccessAttemptAdmin, AccessFailureLogAdmin, AccessLogAdmin
from axes.models import AccessAttempt, AccessFailureLog, AccessLog
from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.db.models import Count, Exists, F, OuterRef
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from unfold.widgets import UnfoldAdminTextInputWidget

from . import services
from .models import Author, Book, Loan, Member


def can_manage_catalogue(request) -> bool:
    return request.user.has_perm("library.change_book")


def can_change_member_status(request) -> bool:
    return request.user.has_perm("library.change_member_status")


def report_bulk_result(modeladmin, request, result, done_verb):
    """One summary message for a bulk action, plus the reasons for skips."""
    if result.succeeded:
        modeladmin.message_user(
            request, f"{result.succeeded} loan(s) {done_verb}.", level=messages.SUCCESS
        )
    if result.failures:
        shown = result.failures[:10]
        more = len(result.failures) - len(shown)
        detail = format_html_join("", "<li>{}</li>", ((f,) for f in shown))
        suffix = f" and {more} more" if more else ""
        modeladmin.message_user(
            request,
            format_html("{} loan(s) skipped{}:<ul>{}</ul>", len(result.failures), suffix, detail),
            level=messages.WARNING,
        )


class LibraryModelAdmin(ModelAdmin):
    """Shared look for every library model page (Unfold theme)."""

    compressed_fields = True  # label and input side by side on wide screens
    warn_unsaved_form = True  # ask before leaving a half-filled form
    list_filter_submit = True  # apply several filters at once


# --- Forms -------------------------------------------------------------------


class CheckoutForm(forms.ModelForm):
    """Validates a new loan against the borrowing rules while holding row locks.

    Used by the Loan add page and by the "Lend a book" inline on a member.
    The admin wraps each add/change view in a transaction, so the locks taken
    here last until the loan is saved by services.checkout_book()."""

    class Meta:
        model = Loan
        fields = ["book", "member", "loaned_on", "due_date", "notes"]

    def clean(self):
        cleaned = super().clean()
        book, member = cleaned.get("book"), cleaned.get("member")
        if self.instance._state.adding and book and member:
            problems = services.checkout_problems(member, book)
            if problems:
                raise forms.ValidationError(problems)
        return cleaned


# --- Filters -----------------------------------------------------------------


class AvailabilityFilter(admin.SimpleListFilter):
    title = "availability"
    parameter_name = "availability"

    def lookups(self, request, model_admin):
        return [("available", "Has a free copy"), ("out", "All copies on loan")]

    def queryset(self, request, queryset):
        # Relies on the annotation added in BookAdmin.get_queryset.
        if self.value() == "available":
            return queryset.filter(annotated_available__gt=0)
        if self.value() == "out":
            return queryset.filter(annotated_available__lte=0)
        return queryset


class PublishedPeriodFilter(admin.SimpleListFilter):
    title = "published"
    parameter_name = "published"
    PERIODS = {
        "pre1950": ("Before 1950", None, 1949),
        "1950-1999": ("1950-1999", 1950, 1999),
        "2000-2014": ("2000-2014", 2000, 2014),
        "2015+": ("2015 or later", 2015, None),
    }

    def lookups(self, request, model_admin):
        return [(key, label) for key, (label, _, _) in self.PERIODS.items()]

    def queryset(self, request, queryset):
        if self.value() not in self.PERIODS:
            return queryset
        _, start, end = self.PERIODS[self.value()]
        if start:
            queryset = queryset.filter(published_year__gte=start)
        if end:
            queryset = queryset.filter(published_year__lte=end)
        return queryset


class MemberOverdueFilter(admin.SimpleListFilter):
    title = "overdue loans"
    parameter_name = "overdue"

    def lookups(self, request, model_admin):
        return [("yes", "Has overdue loans"), ("no", "Nothing overdue")]

    def queryset(self, request, queryset):
        overdue = Exists(Loan.objects.overdue().filter(member=OuterRef("pk")))
        if self.value() == "yes":
            return queryset.filter(overdue)
        if self.value() == "no":
            return queryset.exclude(overdue)
        return queryset


class LoanDueFilter(admin.SimpleListFilter):
    title = "due"
    parameter_name = "due"

    def lookups(self, request, model_admin):
        return [
            ("overdue", "Overdue"),
            ("soon", "Due in the next 3 days"),
            ("returned_late", "Returned late"),
        ]

    def queryset(self, request, queryset):
        today = timezone.localdate()
        if self.value() == "overdue":
            return queryset.overdue(today)
        if self.value() == "soon":
            return queryset.active().filter(
                due_date__gte=today, due_date__lte=today + timedelta(days=3)
            )
        if self.value() == "returned_late":
            return queryset.filter(status=Loan.Status.RETURNED, returned_at__date__gt=F("due_date"))
        return queryset


# --- Inlines -----------------------------------------------------------------


class ReadOnlyLoanInline(TabularInline):
    """Loan history shown inside a book or member page. Never edited here:
    loans change only through the checkout/return/extend services."""

    model = Loan
    extra = 0
    can_delete = False
    show_change_link = True
    show_title = False  # no "Book → Member" caption above every row
    classes = ["collapse"]
    readonly_fields = ["loaned_on", "due_date", "status", "overdue", "late_fee"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @display(description="overdue", label="danger")
    def overdue(self, loan):
        return f"{loan.days_overdue()} d late" if loan.is_overdue else None


class BookLoanHistoryInline(ReadOnlyLoanInline):
    verbose_name_plural = "Loan history"
    fields = ["member", "loaned_on", "due_date", "status", "overdue", "late_fee"]
    readonly_fields = ["member", *ReadOnlyLoanInline.readonly_fields]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("member")


class MemberLoanInline(ReadOnlyLoanInline):
    verbose_name_plural = "Loans (open first)"
    classes = []
    fields = ["book", "loaned_on", "due_date", "status", "overdue", "late_fee"]
    readonly_fields = ["book", *ReadOnlyLoanInline.readonly_fields]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("book").order_by("status", "due_date")


class LendBookInline(TabularInline):
    """Inline editing on the member page: lend books to this member without
    leaving it. Rows are only ever added; saving goes through checkout_book."""

    model = Loan
    form = CheckoutForm
    fields = ["book", "due_date", "notes"]
    autocomplete_fields = ["book"]
    extra = 1
    can_delete = False
    verbose_name = "loan"
    verbose_name_plural = "Lend a book"

    def get_queryset(self, request):
        return super().get_queryset(request).none()

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "notes":  # one line in a table row, not a big text box
            kwargs["widget"] = UnfoldAdminTextInputWidget(attrs={"placeholder": "Optional"})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return self.has_add_permission(request, obj)


# --- Author ------------------------------------------------------------------


@admin.register(Author)
class AuthorAdmin(LibraryModelAdmin):
    list_display = ["name", "born_year", "book_count"]
    search_fields = ["name"]
    list_per_page = 50
    ordering = ["name"]
    fields = ["name", "born_year", "bio", "book_list"]
    readonly_fields = ["book_list"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(annotated_books=Count("books"))

    @admin.display(description="books", ordering="annotated_books")
    def book_count(self, author):
        return author.annotated_books

    @admin.display(description="books in the catalogue")
    def book_list(self, author):
        """The author's books with availability, each linking to the book. Shown
        to every role (librarians see the author page read-only)."""
        if author.pk is None:
            return "Books appear here once the author is saved and added to a book."
        books = Book.objects.with_availability().filter(authors=author).order_by("title")
        if not books:
            return "No books by this author yet."
        rows = format_html_join(
            "",
            '<tr><td><a class="lib-link" href="{}">{}</a></td><td>{}</td><td>{}</td></tr>',
            (
                (
                    reverse("admin:library_book_change", args=[b.pk]),
                    b.title,
                    b.published_year or "-",
                    "withdrawn"
                    if b.is_withdrawn
                    else f"{b.copies_available} of {b.copies_total} on the shelf",
                )
                for b in books
            ),
        )
        return format_html(
            '<table class="lib-table"><thead><tr><th>Title</th><th>Published</th>'
            "<th>Availability</th></tr></thead><tbody>{}</tbody></table>",
            rows,
        )


# --- Book --------------------------------------------------------------------


@admin.register(Book)
class BookAdmin(LibraryModelAdmin):
    list_display_links = ["title"]
    list_filter = ["genre", AvailabilityFilter, PublishedPeriodFilter, "is_withdrawn"]
    search_fields = ["title", "isbn", "authors__name"]
    autocomplete_fields = ["authors"]
    readonly_fields = ["cover_preview", "availability_summary", "created_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    inlines = [BookLoanHistoryInline]
    actions = ["withdraw_books", "restore_books"]
    fieldsets = [
        (None, {"fields": ["title", "authors", "isbn", "genre", "published_year"]}),
        ("Copies", {"fields": ["copies_total", "availability_summary", "is_withdrawn"]}),
        ("Cover", {"fields": ["cover_image", "cover_preview"]}),
        ("Record", {"fields": ["created_at"], "classes": ["collapse"]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).with_availability().prefetch_related("authors")

    def get_list_display(self, request):
        # Librarians answer "is it on the shelf?"; catalogue managers also
        # need the stock and record columns.
        columns = ["cover_thumb", "title", "author_names", "genre", "available"]
        if can_manage_catalogue(request):
            columns += ["copies_total", "isbn", "withdrawn_label", "created_at"]
        return columns

    @admin.display(description="cover")
    def cover_thumb(self, book):
        if not book.cover_image:
            return format_html('<span class="cover-thumb cover-thumb--empty">{}</span>', "–")
        return format_html(
            '<img src="{}" class="cover-thumb" alt="" loading="lazy">', book.cover_image.url
        )

    @admin.display(description="cover")
    def cover_preview(self, book):
        if not book.cover_image:
            return "No cover uploaded."
        return format_html('<img src="{}" class="cover-preview" alt="">', book.cover_image.url)

    @display(description="circulation", ordering="is_withdrawn", label="warning")
    def withdrawn_label(self, book):
        # Only flag the exception; a red cross on every normal book reads as an error.
        return "Withdrawn" if book.is_withdrawn else None

    @admin.display(description="authors")
    def author_names(self, book):
        # Uses the prefetch from get_queryset: no query per row.
        return ", ".join(author.name for author in book.authors.all())

    @admin.display(description="available", ordering="annotated_available")
    def available(self, book):
        css = "badge--ok" if book.copies_available > 0 else "badge--bad"
        return format_html(
            '<span class="badge {}">{} / {}</span>', css, book.copies_available, book.copies_total
        )

    @admin.display(description="availability")
    def availability_summary(self, book):
        if book.pk is None:
            return "-"
        return f"{book.copies_available} of {book.copies_total} on the shelf"

    @admin.action(description="Withdraw selected books from circulation", permissions=["change"])
    def withdraw_books(self, request, queryset):
        updated = queryset.update(is_withdrawn=True)
        self.message_user(request, f"{updated} book(s) withdrawn. Open loans are unaffected.")

    @admin.action(description="Return selected books to circulation", permissions=["change"])
    def restore_books(self, request, queryset):
        updated = queryset.update(is_withdrawn=False)
        self.message_user(request, f"{updated} book(s) back in circulation.")


# --- Member ------------------------------------------------------------------


@admin.register(Member)
class MemberAdmin(LibraryModelAdmin):
    list_display = [
        "full_name",
        "card_number",
        "email",
        "status_badge",
        "active_loans",
        "overdue_badge",
        "fees_due_display",
        "joined_at",
    ]
    list_filter = ["status", MemberOverdueFilter, "joined_at"]
    search_fields = ["full_name", "email", "=card_number"]
    list_per_page = 50
    readonly_fields = ["card_number", "fees_due_display", "open_loans_link"]
    actions = ["suspend_members", "reactivate_members"]
    # Buttons at the top of the member page (Admin role only).
    actions_detail = ["suspend_member", "reactivate_member"]
    inlines = [LendBookInline, MemberLoanInline]

    def get_queryset(self, request):
        return super().get_queryset(request).with_loan_counts()

    def get_fields(self, request, obj=None):
        fields = ["full_name", "email", "status", "joined_at"]
        if obj is None:  # the card number is assigned on save
            return fields
        return ["card_number", *fields, "fees_due_display", "open_loans_link"]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not can_change_member_status(request):
            readonly.append("status")
        return readonly

    def has_change_status_permission(self, request):
        return can_change_member_status(request)

    @display(
        description="status",
        ordering="status",
        label={Member.Status.ACTIVE.label: "success", Member.Status.SUSPENDED.label: "danger"},
    )
    def status_badge(self, member):
        return member.get_status_display()

    @admin.display(description="open loans", ordering="annotated_active_loans")
    def active_loans(self, member):
        return member.active_loan_count

    @display(description="overdue", ordering="annotated_overdue_loans", label="danger")
    def overdue_badge(self, member):
        if not member.overdue_loan_count:
            return None
        return f"{member.overdue_loan_count} overdue"

    @admin.display(description="fees due", ordering="annotated_days_late")
    def fees_due_display(self, member):
        fee = member.fees_due
        return f"€{fee:.2f}" if fee else "-"

    @admin.display(description="return or extend")
    def open_loans_link(self, member):
        url = reverse("admin:library_loan_changelist")
        return format_html(
            '<a class="lib-link" href="{}?member__id__exact={}&status__exact=active">'
            "Open this member's {} active loan(s) to return or extend them</a>",
            url,
            member.pk,
            member.active_loan_count,
        )

    def save_formset(self, request, form, formset, change):
        if formset.model is not Loan:
            return super().save_formset(request, form, formset, change)
        # The only editable Loan inline is LendBookInline (add-only).
        # The admin reads these three lists to write its change-history entry.
        formset.new_objects, formset.changed_objects, formset.deleted_objects = [], [], []
        for loan_form in formset.forms:
            if not loan_form.has_changed() or loan_form.cleaned_data.get("DELETE"):
                continue
            data = loan_form.cleaned_data
            try:
                loan = services.checkout_book(
                    member=form.instance,
                    book=data["book"],
                    due_date=data.get("due_date"),
                    notes=data.get("notes", ""),
                )
                formset.new_objects.append(loan)
            except services.LoanRuleError as exc:
                # Possible when one form lends two books that together break
                # a rule (e.g. the loan cap); the earlier rows are kept.
                self.message_user(request, " ".join(exc.messages), level=messages.ERROR)

    @admin.action(description="Suspend selected members", permissions=["change_status"])
    def suspend_members(self, request, queryset):
        updated = queryset.exclude(status=Member.Status.SUSPENDED).update(
            status=Member.Status.SUSPENDED
        )
        self.message_user(request, f"{updated} member(s) suspended. They can no longer borrow.")

    @admin.action(description="Reactivate selected members", permissions=["change_status"])
    def reactivate_members(self, request, queryset):
        updated = queryset.exclude(status=Member.Status.ACTIVE).update(status=Member.Status.ACTIVE)
        self.message_user(request, f"{updated} member(s) reactivated.")

    # --- single-member buttons ------------------------------------------------

    def get_actions_detail(self, request, object_id):
        """Offer only the button that makes sense: Suspend for an active
        member, Reactivate for a suspended one."""
        buttons = super().get_actions_detail(request, object_id)
        member = Member.objects.filter(pk=object_id).only("status").first()
        hide = (
            "reactivate_member"
            if member and member.status == Member.Status.ACTIVE
            else ("suspend_member")
        )
        return [b for b in buttons if not b.action_name.endswith(hide)]

    @action(
        description="Suspend member",
        url_path="suspend",
        permissions=["library.change_member_status"],
        icon="block",
    )
    def suspend_member(self, request, object_id):
        return self._set_status(request, object_id, Member.Status.SUSPENDED)

    @action(
        description="Reactivate member",
        url_path="reactivate",
        permissions=["library.change_member_status"],
        icon="check_circle",
    )
    def reactivate_member(self, request, object_id):
        return self._set_status(request, object_id, Member.Status.ACTIVE)

    def _set_status(self, request, object_id, status):
        """The button is a link (GET), which must not change data, so it opens
        a confirmation page; only its POST (CSRF-protected) changes the status."""
        member = get_object_or_404(Member, pk=object_id)
        change_url = reverse("admin:library_member_change", args=[member.pk])
        if request.method == "POST":
            member.status = status
            member.save(update_fields=["status"])
            verb = "suspended" if status == Member.Status.SUSPENDED else "reactivated"
            self.message_user(request, f"{member} was {verb}.", level=messages.SUCCESS)
            return redirect(change_url)
        suspending = status == Member.Status.SUSPENDED
        return TemplateResponse(
            request,
            "admin/library/member/status_confirm.html",
            {
                **self.admin_site.each_context(request),
                "title": f"{'Suspend' if suspending else 'Reactivate'} {member}?",
                "opts": self.model._meta,
                "member": member,
                "suspending": suspending,
                "cancel_url": change_url,
            },
        )


# --- Loan --------------------------------------------------------------------


@admin.register(Loan)
class LoanAdmin(LibraryModelAdmin):
    form = CheckoutForm
    list_display = [
        "book",
        "member",
        "loaned_on",
        "due_date",
        "status_label",
        "overdue",
        "late_fee_display",
        "extended",
    ]
    list_select_related = ["book", "member"]
    list_filter = ["status", LoanDueFilter, "extended", "due_date"]
    search_fields = ["book__title", "member__full_name", "member__email", "=member__card_number"]
    autocomplete_fields = ["book", "member"]
    date_hierarchy = "loaned_on"
    list_per_page = 100
    show_full_result_count = False  # skip a second COUNT(*) on a large table
    actions = ["mark_returned", "extend_seven_days"]

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["book", "member", "loaned_on", "due_date", "notes"]
        return [
            "book",
            "member",
            "loaned_on",
            "due_date",
            "status",
            "returned_at",
            "extended",
            "late_fee_display",
            "notes",
        ]

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        # Existing loans change only through the return/extend actions.
        return [
            "book",
            "member",
            "loaned_on",
            "due_date",
            "status",
            "returned_at",
            "extended",
            "late_fee_display",
        ]

    @admin.display(boolean=True, description="overdue")
    def overdue(self, loan):
        return loan.is_overdue

    @display(
        description="status",
        ordering="status",
        label={Loan.Status.ACTIVE.label: "info", Loan.Status.RETURNED.label: "success"},
    )
    def status_label(self, loan):
        return loan.get_status_display()

    @admin.display(description="late fee")
    def late_fee_display(self, loan):
        fee = loan.late_fee
        return f"€{fee:.2f}" if fee else "-"

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        loan = services.checkout_book(
            member=obj.member,
            book=obj.book,
            loaned_on=obj.loaned_on,
            due_date=obj.due_date,
            notes=obj.notes,
        )
        # The admin keeps using `obj` (log entry, redirect); point it at the
        # row the service created.
        obj.pk = loan.pk
        obj._state.adding = False
        obj._state.db = loan._state.db

    @admin.action(description="Mark selected loans as returned", permissions=["change"])
    def mark_returned(self, request, queryset):
        result = services.apply_to_each(
            queryset.select_related("book", "member"), services.return_loan
        )
        report_bulk_result(self, request, result, "marked as returned")

    @admin.action(description="Extend due date by 7 days", permissions=["change"])
    def extend_seven_days(self, request, queryset):
        result = services.apply_to_each(
            queryset.select_related("book", "member"), services.extend_loan
        )
        report_bulk_result(self, request, result, "extended by 7 days")


# --- Users -------------------------------------------------------------------


admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class StaffUserAdmin(BaseUserAdmin, ModelAdmin):
    """Members of the Admin group manage staff accounts, but only a superuser
    can hand out superuser status or per-user permissions. Otherwise an Admin
    could grant themselves anything."""

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += ["is_superuser", "user_permissions", "last_login", "date_joined"]
        return readonly


@admin.register(Group)
class RoleAdmin(BaseGroupAdmin, ModelAdmin):
    pass


# The lockout log from django-axes, re-registered so it uses the same theme.
for model, base in [
    (AccessAttempt, AccessAttemptAdmin),
    (AccessFailureLog, AccessFailureLogAdmin),
    (AccessLog, AccessLogAdmin),
]:
    if admin.site.is_registered(model):  # only the ones axes itself shows
        admin.site.unregister(model)
        admin.site.register(model, type(f"Themed{base.__name__}", (base, ModelAdmin), {}))
