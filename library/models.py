"""Domain model for the lending library.

Rules that must hold no matter how data is written live in the database
(constraints). Rules that depend on other rows (free copies, loan cap) live in
Loan.borrowing_rule_violations() and are enforced under row locks in
library/services.py. Derived values (copies available, overdue, late fee) are
computed, never stored, so they cannot drift.
"""

import os
import secrets
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Count, DurationField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce, Lower
from django.urls import reverse
from django.utils import timezone

from .validators import (
    normalize_isbn,
    validate_cover_image,
    validate_isbn13,
    validate_not_future_year,
)

LOAN_PERIOD_DAYS = 14
MAX_EXTENSION_DAYS = 7
LATE_FEE_PER_DAY = Decimal("0.50")  # euros per day overdue


def cover_upload_to(instance, filename):
    """Store covers under a random name. The uploader's filename is never used
    as a path, so it cannot collide, overwrite, or carry path tricks."""
    ext = os.path.splitext(filename)[1].lower()
    return f"covers/{uuid.uuid4().hex}{ext}"


# --- Author ----------------------------------------------------------------


class Author(models.Model):
    name = models.CharField(
        max_length=200,
        db_index=True,
        help_text="Full name as printed on the book, e.g. 'Ursula K. Le Guin'.",
    )
    bio = models.TextField(
        blank=True, help_text="Optional short biography shown to staff. Plain text."
    )
    born_year = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1000), validate_not_future_year],
        help_text="Year of birth, four digits (e.g. 1929). Leave empty if unknown.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# --- Book ------------------------------------------------------------------


class Genre(models.TextChoices):
    FICTION = "fiction", "Fiction"
    SCIFI = "scifi", "Science fiction"
    FANTASY = "fantasy", "Fantasy"
    MYSTERY = "mystery", "Mystery"
    HISTORY = "history", "History"
    SCIENCE = "science", "Science"
    BIOGRAPHY = "biography", "Biography"
    CHILDREN = "children", "Children"
    OTHER = "other", "Other"


class BookQuerySet(models.QuerySet):
    def with_availability(self):
        """Annotate copies on loan and copies available in one query.

        Used by the admin changelist so the column is sortable and does not
        cost one query per row."""
        return self.annotate(
            annotated_on_loan=Count(
                "loans", filter=Q(loans__status=Loan.Status.ACTIVE), distinct=True
            ),
        ).annotate(annotated_available=F("copies_total") - F("annotated_on_loan"))


class Book(models.Model):
    title = models.CharField(
        max_length=300, db_index=True, help_text="Title as printed on the cover."
    )
    isbn = models.CharField(
        "ISBN",
        max_length=17,
        unique=True,
        validators=[validate_isbn13],
        help_text="13-digit ISBN from the back cover or copyright page. Hyphens are optional "
        "and removed on save.",
    )
    authors = models.ManyToManyField(
        Author,
        related_name="books",
        help_text="One or more authors. Search by name; add a missing author first.",
    )
    published_year = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1450), validate_not_future_year],
        help_text="Year this edition was published, e.g. 2019. Leave empty if unknown.",
    )
    genre = models.CharField(
        max_length=20,
        choices=Genre.choices,
        default=Genre.OTHER,
        db_index=True,
        help_text="Main shelf category. Pick 'Other' if none fits.",
    )
    copies_total = models.PositiveSmallIntegerField(
        "total copies",
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="How many physical copies the library owns (1-100). Cannot go below the "
        "number currently on loan.",
    )
    is_withdrawn = models.BooleanField(
        "withdrawn",
        default=False,
        help_text="Withdrawn books stay in the catalogue for history but cannot be loaned.",
    )
    cover_image = models.ImageField(
        upload_to=cover_upload_to,
        blank=True,
        validators=[validate_cover_image],
        help_text="Optional cover photo: JPG, PNG or WebP, max 2 MB and 4000x4000 px.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True, help_text="When the book was added to the catalogue. Set automatically."
    )

    objects = BookQuerySet.as_manager()

    class Meta:
        ordering = ["title"]
        constraints = [
            models.CheckConstraint(condition=Q(copies_total__gte=1), name="book_has_a_copy"),
        ]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        self.isbn = normalize_isbn(self.isbn)
        super().save(*args, **kwargs)

    @property
    def cover_url(self) -> str:
        """Same-origin URL for the cover (see library/views.py), or "" if none."""
        if not self.cover_image:
            return ""
        return reverse("cover", args=[os.path.basename(self.cover_image.name)])

    @property
    def copies_on_loan(self) -> int:
        if hasattr(self, "annotated_on_loan"):
            return self.annotated_on_loan
        if self.pk is None:
            return 0
        return self.loans.filter(status=Loan.Status.ACTIVE).count()

    @property
    def copies_available(self) -> int:
        if hasattr(self, "annotated_available"):
            return self.annotated_available
        return self.copies_total - self.copies_on_loan

    def clean(self):
        if self.isbn:
            self.isbn = normalize_isbn(self.isbn)
        if self.pk and self.copies_total is not None and self.copies_total < self.copies_on_loan:
            raise ValidationError(
                {
                    "copies_total": f"{self.copies_on_loan} copies are on loan right now; "
                    "total copies cannot be lower than that."
                }
            )


# --- Member ----------------------------------------------------------------


def new_card_number() -> str:
    """Random 8-digit library card number, e.g. LIB-40217735. Random rather than
    sequential so card numbers do not reveal how many members there are."""
    return f"LIB-{secrets.randbelow(10**8):08d}"


class MemberQuerySet(models.QuerySet):
    def with_loan_counts(self):
        """Open, overdue and fees-due figures per member in one query.

        Fees due = late fee for every day past due on books the member still
        has. Stored as total days late (Postgres date arithmetic); the euro
        amount is derived in Member.fees_due, so sorting by days = sorting
        by fee."""
        today = timezone.localdate()
        active = Q(loans__status=Loan.Status.ACTIVE)
        overdue = active & Q(loans__due_date__lt=today)
        days_late = ExpressionWrapper(
            Value(today, output_field=models.DateField()) - F("loans__due_date"),
            output_field=DurationField(),
        )
        return self.annotate(
            annotated_active_loans=Count("loans", filter=active),
            annotated_overdue_loans=Count("loans", filter=overdue),
            # Zero (not NULL) for members who owe nothing, so sorting works both ways.
            annotated_days_late=Coalesce(
                Sum(days_late, filter=overdue), Value(timedelta(0), output_field=DurationField())
            ),
        )

    def by_fees_due(self):
        """Members who owe something, highest bill first."""
        return (
            self.with_loan_counts()
            .filter(annotated_days_late__gt=timedelta(0))
            .order_by(F("annotated_days_late").desc(), "full_name")
        )


class Member(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    card_number = models.CharField(
        max_length=12,
        unique=True,
        default=new_card_number,
        editable=False,
        help_text="Library card number (e.g. LIB-40217735), printed on the member's card. "
        "Assigned automatically; search by it to find a member at the desk.",
    )
    full_name = models.CharField(
        max_length=200, db_index=True, help_text="Member's full name as on their library card."
    )
    email = models.EmailField(
        help_text="Contact email. Must be unique; case is ignored (Ann@x.com = ann@x.com).",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
        help_text="Suspended members cannot borrow. Only administrators can change this.",
    )
    joined_at = models.DateField(
        "joined on", default=timezone.localdate, help_text="Date the membership started."
    )

    objects = MemberQuerySet.as_manager()

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="member_email_ci_unique"),
        ]
        permissions = [
            ("change_member_status", "Can suspend or reactivate members"),
        ]

    def __str__(self):
        return self.full_name

    def clean(self):
        if self.email:
            self.email = self.email.strip()
            clash = Member.objects.filter(email__iexact=self.email).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError({"email": "Another member already uses this email."})

    @property
    def active_loan_count(self) -> int:
        if hasattr(self, "annotated_active_loans"):
            return self.annotated_active_loans
        if self.pk is None:
            return 0
        return self.loans.filter(status=Loan.Status.ACTIVE).count()

    @property
    def overdue_loan_count(self) -> int:
        if hasattr(self, "annotated_overdue_loans"):
            return self.annotated_overdue_loans
        if self.pk is None:
            return 0
        return self.loans.overdue().count()

    @property
    def fees_due(self) -> Decimal:
        """Late fees accrued on books this member still has, as of today."""
        if hasattr(self, "annotated_days_late"):
            days = self.annotated_days_late.days
        elif self.pk is None:
            days = 0
        else:
            days = sum(loan.days_overdue() for loan in self.loans.overdue())
        return LATE_FEE_PER_DAY * days


# --- Loan ------------------------------------------------------------------


def default_due_date() -> date:
    return timezone.localdate() + timedelta(days=LOAN_PERIOD_DAYS)


class LoanQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=Loan.Status.ACTIVE)

    def overdue(self, today: date | None = None):
        today = today or timezone.localdate()
        return self.filter(status=Loan.Status.ACTIVE, due_date__lt=today)


class Loan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "On loan"
        RETURNED = "returned", "Returned"

    book = models.ForeignKey(
        Book,
        on_delete=models.PROTECT,  # loan history must survive; withdraw books instead
        related_name="loans",
        help_text="The book being lent. It must have a free copy and not be withdrawn.",
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="loans",
        help_text="Who borrows the book. Must be active and hold fewer than the loan limit.",
    )
    loaned_on = models.DateField(
        default=timezone.localdate, help_text="Date the book left the library. Defaults to today."
    )
    due_date = models.DateField(
        default=default_due_date,
        help_text=f"Date the book must be back. Defaults to {LOAN_PERIOD_DAYS} days after the "
        "loan date and must be after it.",
    )
    returned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the book came back. Set automatically by the 'Mark as returned' action.",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
        help_text="'On loan' until returned. 'Returned' is final and cannot be undone.",
    )
    extended = models.BooleanField(
        default=False,
        help_text=f"True once the due date was extended. A loan can be extended once, by up to "
        f"{MAX_EXTENSION_DAYS} days, and not after it is overdue.",
    )
    notes = models.TextField(
        blank=True, help_text="Optional staff notes, e.g. condition of the book at checkout."
    )

    objects = LoanQuerySet.as_manager()

    class Meta:
        ordering = ["-loaned_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(due_date__gt=F("loaned_on")), name="loan_due_after_loan_date"
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", returned_at__isnull=True)
                    | Q(status="returned", returned_at__isnull=False)
                ),
                name="loan_returned_at_matches_status",
            ),
            # A member cannot hold two copies of the same book at once.
            models.UniqueConstraint(
                fields=["book", "member"],
                condition=Q(status="active"),
                name="loan_one_active_per_book_member",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_date"], name="loan_status_due_idx"),
            models.Index(fields=["member", "status"], name="loan_member_status_idx"),
            models.Index(fields=["book", "status"], name="loan_book_status_idx"),
        ]

    def __str__(self):
        return f"{self.book} → {self.member}"

    # --- computed values ---------------------------------------------------

    def days_overdue(self, today: date | None = None) -> int:
        """Days past the due date: up to today for open loans, up to the
        return date for returned ones."""
        if self.due_date is None:
            return 0
        if self.status == self.Status.RETURNED and self.returned_at:
            end = timezone.localtime(self.returned_at).date()
        else:
            end = today or timezone.localdate()
        return max(0, (end - self.due_date).days)

    @property
    def is_overdue(self) -> bool:
        return self.status == self.Status.ACTIVE and self.days_overdue() > 0

    @property
    def late_fee(self) -> Decimal:
        return LATE_FEE_PER_DAY * self.days_overdue()

    # --- rules -------------------------------------------------------------

    @staticmethod
    def borrowing_rule_violations(member: Member, book: Book) -> list[str]:
        """Every reason this member may not borrow this book right now.

        Pure rule check; callers that need it to be race-free use
        services.checkout_problems(), which takes row locks first."""
        problems = []
        if member.status == Member.Status.SUSPENDED:
            problems.append(f"{member} is suspended and cannot borrow.")
        # A member being created in the same form has no loans yet.
        if member.pk is not None:
            active = Loan.objects.filter(member=member, status=Loan.Status.ACTIVE)
            if active.count() >= settings.MAX_ACTIVE_LOANS:
                problems.append(f"{member} already has {settings.MAX_ACTIVE_LOANS} books on loan.")
            if active.filter(book=book).exists():
                problems.append(f"{member} already has a copy of '{book}'.")
        if book.is_withdrawn:
            problems.append(f"'{book}' is withdrawn from circulation.")
        on_loan = Loan.objects.filter(book=book, status=Loan.Status.ACTIVE).count()
        if on_loan >= book.copies_total:
            problems.append(f"All {book.copies_total} copies of '{book}' are on loan.")
        return problems

    def clean(self):
        if self.loaned_on and self.due_date and self.due_date <= self.loaned_on:
            raise ValidationError({"due_date": "Due date must be after the loan date."})

        if self._state.adding:
            # Borrowing rules need row locks, so they are checked by
            # services.checkout_problems(), not here.
            if self.status != self.Status.ACTIVE:
                raise ValidationError({"status": "A new loan always starts as 'On loan'."})
            return

        previous = Loan.objects.filter(pk=self.pk).values("status", "book_id", "member_id").first()
        if previous is None:
            return
        if previous["status"] == self.Status.RETURNED and self.status != self.Status.RETURNED:
            raise ValidationError({"status": "A returned loan cannot be reopened."})
        if (previous["book_id"], previous["member_id"]) != (self.book_id, self.member_id):
            raise ValidationError("Book and member cannot be changed on an existing loan.")
