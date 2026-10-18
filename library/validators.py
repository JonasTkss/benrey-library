"""Field validators. Kept as plain functions so migrations reference them by
import path instead of freezing values (like "the current year") into the
migration file."""

import os

from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

COVER_MAX_BYTES = 2 * 1024 * 1024
COVER_MAX_PIXELS = 4000
COVER_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# Formats as Pillow names them after reading the file content.
COVER_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def normalize_isbn(value: str) -> str:
    """Strip the hyphens and spaces people type, so '978-0-13-468599-1' and
    '9780134685991' are stored (and kept unique) as the same value."""
    return value.replace("-", "").replace(" ", "").strip()


def validate_isbn13(value: str) -> None:
    digits = normalize_isbn(value)
    if len(digits) != 13 or not digits.isdigit():
        raise ValidationError("ISBN must be 13 digits (hyphens are allowed).", code="isbn_format")
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
    if (10 - total % 10) % 10 != int(digits[12]):
        raise ValidationError("ISBN check digit is wrong; check for a typo.", code="isbn_checksum")


def validate_not_future_year(value: int) -> None:
    if value is not None and value > timezone.localdate().year:
        raise ValidationError("Year cannot be in the future.", code="future_year")


def validate_cover_image(file) -> None:
    """Accept only real JPEG/PNG/WebP images up to 2 MB and 4000px.

    The extension check is a cheap first filter; the Pillow check reads the
    actual bytes, so an .exe renamed to .png is rejected. SVG is not in the
    allow-list because it can carry script.
    """
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in COVER_ALLOWED_EXTENSIONS:
        raise ValidationError(
            "Upload a JPG, PNG or WebP image.", code="cover_extension", params={"ext": ext}
        )
    if file.size > COVER_MAX_BYTES:
        raise ValidationError("Cover image must be 2 MB or smaller.", code="cover_size")

    position = file.tell() if hasattr(file, "tell") else 0
    try:
        file.seek(0)
        with Image.open(file) as image:
            image_format = image.format
            width, height = image.size
            image.verify()  # detects truncated/corrupt files
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValidationError("File is not a valid image.", code="cover_content") from exc
    finally:
        file.seek(position)

    if image_format not in COVER_ALLOWED_FORMATS:
        raise ValidationError("Upload a JPG, PNG or WebP image.", code="cover_format")
    if width > COVER_MAX_PIXELS or height > COVER_MAX_PIXELS:
        raise ValidationError(
            "Cover image must be at most 4000x4000 pixels.", code="cover_dimensions"
        )
