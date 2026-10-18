import factory
from factory.django import DjangoModelFactory

from library.models import Author, Book, Genre, Loan, Member


def isbn13(n: int) -> str:
    """A valid ISBN-13 built from a sequence number (978 prefix + checksum)."""
    body = f"978{n:09d}"
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


class AuthorFactory(DjangoModelFactory):
    class Meta:
        model = Author

    name = factory.Sequence(lambda n: f"Author {n}")


class BookFactory(DjangoModelFactory):
    class Meta:
        model = Book
        skip_postgeneration_save = True

    title = factory.Sequence(lambda n: f"Book {n}")
    isbn = factory.Sequence(isbn13)
    genre = Genre.FICTION
    copies_total = 1

    @factory.post_generation
    def authors(self, create, extracted, **kwargs):
        if create:
            self.authors.set(extracted if extracted is not None else [AuthorFactory()])


class MemberFactory(DjangoModelFactory):
    class Meta:
        model = Member

    full_name = factory.Sequence(lambda n: f"Member {n}")
    email = factory.Sequence(lambda n: f"member{n}@example.com")


class LoanFactory(DjangoModelFactory):
    """Inserts directly, bypassing the checkout rules. Use services.checkout_book
    in tests that are about the rules."""

    class Meta:
        model = Loan

    book = factory.SubFactory(BookFactory)
    member = factory.SubFactory(MemberFactory)
