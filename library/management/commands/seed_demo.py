"""Fill the database with realistic demo data.

    python manage.py seed_demo --reset --seed 42          # full set
    python manage.py seed_demo --reset --small             # fast, for local/CI

Every loan respects the same rules the app enforces (free copies, loan cap,
one active copy per member), so the data never shows impossible states.
"""

import io
import os
import random
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from faker import Faker
from PIL import Image, ImageDraw, ImageFont

from library.models import (
    LOAN_PERIOD_DAYS,
    MAX_EXTENSION_DAYS,
    Author,
    Book,
    Genre,
    Loan,
    Member,
)

FULL = {"authors": 300, "books": 2000, "members": 600, "loans": 6000, "covers": 100}
SMALL = {"authors": 20, "books": 60, "members": 30, "loans": 150, "covers": 3}
ACTIVE_SHARE = 0.2  # share of loans still open; the rest are returned history

TITLE_PATTERNS = [
    "The {adj} {noun}",
    "{noun} of {other}",
    "The {noun} and the {other}",
    "{adj} {noun}",
    "{noun} for {name}",
    "The Last {noun}",
    "{name}'s {noun}",
    "Beyond the {adj} {noun}",
]
ADJECTIVES = [
    "Silent",
    "Hidden",
    "Burning",
    "Silver",
    "Distant",
    "Broken",
    "Northern",
    "Golden",
    "Quiet",
    "Endless",
    "Forgotten",
    "Crimson",
    "Winter",
    "Wild",
    "Hollow",
    "Bright",
    "Lost",
    "Secret",
    "Iron",
    "Paper",
    "Glass",
    "Salt",
    "Midnight",
    "Summer",
]
NOUNS = [
    "River",
    "Garden",
    "Kingdom",
    "Lighthouse",
    "Orchard",
    "Archive",
    "Harbor",
    "Forest",
    "Machine",
    "Station",
    "Letter",
    "Mountain",
    "Island",
    "Library",
    "Clockmaker",
    "Map",
    "Storm",
    "Tide",
    "Voyage",
    "Shadow",
    "Bridge",
    "Engine",
    "City",
    "Promise",
    "Window",
    "Compass",
    "Season",
    "Sparrow",
    "Atlas",
    "Signal",
    "Ember",
    "Valley",
    "Theory",
]

DEMO_USERS = [
    ("admin_demo", "Admin", "DEMO_ADMIN_PASSWORD"),
    ("librarian_demo", "Librarian", "DEMO_LIBRARIAN_PASSWORD"),
]


def isbn13(n: int) -> str:
    body = f"978{n:09d}"
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


class Command(BaseCommand):
    help = "Seed demo authors, books, members, loans, covers and the two demo users."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete all library data first.")
        parser.add_argument("--seed", type=int, default=42, help="Random seed (deterministic).")
        parser.add_argument("--small", action="store_true", help="Tiny data set for tests.")
        parser.add_argument("--covers", type=int, help="Number of generated cover images.")
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required to --reset when DEBUG is off (i.e. on the deployed app).",
        )

    def handle(self, *args, **opts):
        sizes = dict(SMALL if opts["small"] else FULL)
        if opts["covers"] is not None:
            sizes["covers"] = opts["covers"]

        if opts["reset"]:
            if not settings.DEBUG and not opts["allow_production"]:
                raise CommandError(
                    "Refusing to wipe data with DEBUG off. Add --allow-production if you "
                    "really mean to reset the deployed database."
                )
            self.reset()
        elif Book.objects.exists():
            raise CommandError("Library data already exists. Use --reset to replace it.")

        # Seeded PRNG for reproducible demo data; nothing security-related.
        self.rng = random.Random(opts["seed"])  # nosec B311
        self.fake = Faker()
        self.fake.seed_instance(opts["seed"])
        self.today = timezone.localdate()

        with transaction.atomic():
            authors = self.create_authors(sizes["authors"])
            books = self.create_books(sizes["books"], authors)
            members = self.create_members(sizes["members"])
            loans = self.create_loans(sizes["loans"], books, members)
        self.stdout.write(
            f"Created {len(authors)} authors, {len(books)} books, {len(members)} members, "
            f"{loans} loans."
        )
        # Uploads happen outside the transaction: a failed upload should not
        # roll back the data, and files cannot be rolled back anyway.
        covered = self.attach_covers(books, sizes["covers"])
        self.stdout.write(f"Uploaded {covered} cover images to {self.storage_name()}.")
        self.create_demo_users()

    # --- reset ---------------------------------------------------------------

    def reset(self):
        cover_names = list(
            Book.objects.exclude(cover_image="").values_list("cover_image", flat=True)
        )
        with transaction.atomic():
            Loan.objects.all().delete()  # loans first: books/members are PROTECTed
            Book.objects.all().delete()
            Author.objects.all().delete()
            Member.objects.all().delete()
        storage = Book._meta.get_field("cover_image").storage
        for name in cover_names:
            storage.delete(name)
        self.stdout.write(f"Reset: removed library data and {len(cover_names)} cover files.")

    # --- data ----------------------------------------------------------------

    def create_authors(self, count):
        authors = [
            Author(
                name=self.fake.unique.name(),
                bio=self.fake.paragraph(nb_sentences=2) if self.rng.random() < 0.6 else "",
                born_year=self.rng.randint(1890, 1995) if self.rng.random() < 0.8 else None,
            )
            for _ in range(count)
        ]
        return Author.objects.bulk_create(authors)

    def book_title(self):
        adjective, noun, other = (
            self.rng.choice(ADJECTIVES),
            self.rng.choice(NOUNS),
            self.rng.choice(NOUNS),
        )
        pattern = self.rng.choice(TITLE_PATTERNS)
        return pattern.format(adj=adjective, noun=noun, other=other, name=self.fake.first_name())

    def create_books(self, count, authors):
        genres = [g.value for g in Genre]
        start = self.rng.randint(10_000, 500_000)
        books = [
            Book(
                title=self.book_title(),
                isbn=isbn13(start + i),
                published_year=self.rng.randint(1925, self.today.year),
                genre=self.rng.choice(genres),
                copies_total=self.rng.choices([1, 2, 3, 4], weights=[45, 30, 15, 10])[0],
                is_withdrawn=self.rng.random() < 0.02,
            )
            for i in range(count)
        ]
        books = Book.objects.bulk_create(books)

        # Spread "added to catalogue" over two years so date drill-down is useful.
        for book in books:
            days_ago = self.rng.randint(0, 730)
            book.created_at = timezone.now() - timedelta(days=days_ago, minutes=book.pk % 1440)
        Book.objects.bulk_update(books, ["created_at"], batch_size=500)

        through = Book.authors.through
        links = []
        for book in books:
            k = self.rng.choices([1, 2, 3], weights=[80, 17, 3])[0]
            for author in self.rng.sample(authors, k):
                links.append(through(book_id=book.pk, author_id=author.pk))
        through.objects.bulk_create(links, batch_size=2000)
        return books

    def create_members(self, count):
        members = []
        for i in range(count):
            first, last = self.fake.first_name(), self.fake.last_name()
            members.append(
                Member(
                    full_name=f"{first} {last}",
                    email=f"{first}.{last}.{i}@example.org".lower(),
                    status=Member.Status.SUSPENDED
                    if self.rng.random() < 0.04
                    else Member.Status.ACTIVE,
                    joined_at=self.today - timedelta(days=self.rng.randint(30, 1500)),
                )
            )
        return Member.objects.bulk_create(members)

    def create_loans(self, count, books, members):
        active_target = int(count * ACTIVE_SHARE)
        loans = []

        # Returned history. Most of it is recent (last ~10 weeks) so that the
        # daily return rate roughly matches the lending rate, like a library in
        # steady state; the rest is a thin tail going back two years.
        for _ in range(count - active_target):
            recent = self.rng.random() < 0.75
            days_ago = self.rng.randint(2, 75) if recent else self.rng.randint(76, 730)
            loaned_on = self.today - timedelta(days=days_ago)
            extended = self.rng.random() < 0.1
            due = loaned_on + timedelta(
                days=LOAN_PERIOD_DAYS + (MAX_EXTENSION_DAYS if extended else 0)
            )
            # Most come back on time, some late; never in the future.
            kept_days = self.rng.choices(
                [self.rng.randint(3, 14), self.rng.randint(15, 30)], weights=[80, 20]
            )[0]
            if kept_days > days_ago:  # would be in the future: spread it over the past days
                kept_days = self.rng.randint(1, days_ago)
            returned_on = loaned_on + timedelta(days=kept_days)
            loans.append(
                Loan(
                    book=self.rng.choice(books),
                    member=self.rng.choice(members),
                    loaned_on=loaned_on,
                    due_date=due,
                    status=Loan.Status.RETURNED,
                    returned_at=self.aware(returned_on),
                    extended=extended,
                )
            )

        # Open loans: obey copies, the loan cap and one copy per member.
        on_loan = {book.pk: 0 for book in books}
        held = {member.pk: set() for member in members}
        lendable_books = [b for b in books if not b.is_withdrawn]
        borrowers = [m for m in members if m.status == Member.Status.ACTIVE]
        made, attempts = 0, 0
        while made < active_target and attempts < active_target * 20:
            attempts += 1
            book, member = self.rng.choice(lendable_books), self.rng.choice(borrowers)
            if (
                on_loan[book.pk] >= book.copies_total
                or len(held[member.pk]) >= settings.MAX_ACTIVE_LOANS
                or book.pk in held[member.pk]
            ):
                continue
            # Most were lent in the last two weeks; roughly 1 in 8 is overdue.
            recent = self.rng.random() < 0.88
            days_ago = self.rng.randint(0, LOAN_PERIOD_DAYS) if recent else self.rng.randint(15, 45)
            loaned_on = self.today - timedelta(days=days_ago)
            loans.append(
                Loan(
                    book=book,
                    member=member,
                    loaned_on=loaned_on,
                    due_date=loaned_on + timedelta(days=LOAN_PERIOD_DAYS),
                )
            )
            on_loan[book.pk] += 1
            held[member.pk].add(book.pk)
            made += 1

        Loan.objects.bulk_create(loans, batch_size=1000)
        return len(loans)

    def aware(self, day):
        hour = self.rng.randint(9, 18)
        return timezone.make_aware(datetime.combine(day, time(hour, self.rng.randint(0, 59))))

    # --- covers --------------------------------------------------------------

    def storage_name(self):
        return settings.STORAGES["default"]["BACKEND"].rsplit(".", 1)[-1]

    def attach_covers(self, books, count):
        chosen = self.rng.sample(books, min(count, len(books)))
        for book in chosen:
            book.cover_image.save(
                "cover.jpg", ContentFile(self.cover_bytes(book.title)), save=False
            )
        Book.objects.bulk_update(chosen, ["cover_image"])
        return len(chosen)

    def cover_bytes(self, title):
        """A 300x450 gradient cover with the title, small enough to load fast."""
        width, height = 300, 450
        top = tuple(self.rng.randint(30, 200) for _ in range(3))
        bottom = tuple(max(0, c - 60) for c in top)
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            mix = y / height
            draw.line(
                [(0, y), (width, y)],
                fill=tuple(int(a + (b - a) * mix) for a, b in zip(top, bottom, strict=True)),
            )
        font = ImageFont.load_default(size=26)
        lines, line = [], ""
        for word in title.split():
            if len(line) + len(word) > 14:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines.append(line)
        for i, text in enumerate(lines[:5]):
            draw.text((24, 60 + i * 34), text, fill="white", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()

    # --- users ---------------------------------------------------------------

    def create_demo_users(self):
        """Demo staff accounts. Passwords come only from environment variables
        and are never printed."""
        for username, role, env_var in DEMO_USERS:
            password = os.environ.get(env_var, "")
            if not password:
                self.stdout.write(self.style.WARNING(f"{env_var} not set; skipped {username}."))
                continue
            user = User.objects.filter(username=username).first() or User(username=username)
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(f"{env_var} is too weak: {' '.join(exc.messages)}") from exc
            user.is_staff = True
            user.is_superuser = False
            user.set_password(password)
            user.save()
            user.groups.set([Group.objects.get(name=role)])
            self.stdout.write(f"Demo user {username} ready ({role}).")
