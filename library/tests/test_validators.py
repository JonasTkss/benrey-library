import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from library.models import cover_upload_to
from library.validators import validate_cover_image, validate_isbn13


def png_bytes(size=(40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "teal").save(buf, format="PNG")
    return buf.getvalue()


def upload(name, content):
    return SimpleUploadedFile(name, content, content_type="image/png")


@pytest.mark.parametrize("isbn", ["9780134685991", "978-0-13-468599-1", "978 0 13 468599 1"])
def test_valid_isbn13(isbn):
    validate_isbn13(isbn)


@pytest.mark.parametrize("isbn", ["9780134685990", "978013468599", "97801346859X1", ""])
def test_invalid_isbn13(isbn):
    with pytest.raises(ValidationError):
        validate_isbn13(isbn)


def test_real_png_is_accepted():
    validate_cover_image(upload("cover.png", png_bytes()))


def test_executable_renamed_to_png_is_rejected():
    with pytest.raises(ValidationError) as exc:
        validate_cover_image(upload("cover.png", b"MZ\x90\x00 this is not an image" * 10))
    assert exc.value.code == "cover_content"


def test_svg_is_rejected():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(ValidationError) as exc:
        validate_cover_image(upload("cover.svg", svg))
    assert exc.value.code == "cover_extension"


def test_oversize_file_is_rejected():
    big = upload("cover.png", png_bytes() + b"\0" * (2 * 1024 * 1024))
    with pytest.raises(ValidationError) as exc:
        validate_cover_image(big)
    assert exc.value.code == "cover_size"


def test_gif_content_with_png_name_is_rejected():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="GIF")
    with pytest.raises(ValidationError) as exc:
        validate_cover_image(upload("cover.png", buf.getvalue()))
    assert exc.value.code == "cover_format"


def test_huge_dimensions_are_rejected():
    with pytest.raises(ValidationError) as exc:
        validate_cover_image(upload("cover.png", png_bytes(size=(4001, 10))))
    assert exc.value.code == "cover_dimensions"


def test_stored_filename_is_random_not_the_upload_name():
    path = cover_upload_to(None, "../../etc/My Holiday Photo.PNG")
    assert path.startswith("covers/")
    assert path.endswith(".png")
    assert "Holiday" not in path and ".." not in path
    assert len(path) == len("covers/") + 32 + len(".png")
