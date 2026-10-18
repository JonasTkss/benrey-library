"""Cover images, served from the app's own URL.

The bucket stays private and its address never reaches the browser: the
network tab only ever shows this app. Files are streamed from object storage,
so nothing is written to the container's disk.
"""

import re

from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden
from django.views.decorators.http import require_GET

from .models import Book

# Exactly what cover_upload_to() produces: a UUID hex name with an image
# extension. Anything else (paths, "..", other bucket keys) is a 404.
COVER_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|webp)$")
CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
# The name is random and never reused, so the file behind a URL never changes.
CACHE_FOREVER = "private, max-age=31536000, immutable"


@require_GET
def cover(request, name):
    user = request.user
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return HttpResponseForbidden("Staff login required.")
    match = COVER_NAME.match(name)
    if not match:
        raise Http404
    path = f"covers/{name}"
    # Only files that belong to a book: the endpoint cannot be used to read
    # arbitrary objects from the bucket.
    if not Book.objects.filter(cover_image=path).exists():
        raise Http404

    etag = f'"{name}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
    else:
        storage = Book._meta.get_field("cover_image").storage
        try:
            handle = storage.open(path, "rb")
        except (FileNotFoundError, OSError) as exc:
            raise Http404 from exc
        response = FileResponse(handle, content_type=CONTENT_TYPES[match.group(1)])
    response["ETag"] = etag
    response["Cache-Control"] = CACHE_FOREVER
    return response
