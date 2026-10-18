import os

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from .factories import BookFactory
from .test_validators import png_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture
def book_with_cover():
    book = BookFactory()
    book.cover_image.save("anything.png", ContentFile(png_bytes()), save=True)
    return book


def test_cover_is_served_from_the_app_with_long_cache(librarian_client, book_with_cover):
    url = book_with_cover.cover_url
    assert url.startswith("/covers/") and "storage" not in url
    response = librarian_client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert "immutable" in response["Cache-Control"] and "private" in response["Cache-Control"]
    assert b"".join(response.streaming_content) == png_bytes()


def test_unchanged_cover_is_a_304(librarian_client, book_with_cover):
    first = librarian_client.get(book_with_cover.cover_url)
    again = librarian_client.get(book_with_cover.cover_url, HTTP_IF_NONE_MATCH=first["ETag"])
    assert again.status_code == 304


def test_covers_need_a_staff_login(client, book_with_cover):
    assert client.get(book_with_cover.cover_url).status_code == 403


@pytest.mark.parametrize(
    "name",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "not-a-cover.png",
        "0123456789abcdef0123456789abcdef.svg",
        "0123456789abcdef0123456789abcdef.png",  # right shape, but no book has it
    ],
)
def test_only_real_cover_files_are_served(librarian_client, name):
    assert librarian_client.get(f"/covers/{name}").status_code == 404


def test_admin_uses_the_same_origin_url(admin_client, book_with_cover):
    page = admin_client.get(reverse("admin:library_book_changelist")).content.decode()
    assert book_with_cover.cover_url in page
    assert os.path.basename(book_with_cover.cover_image.name) in page
