from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(request):
    """Liveness + DB check for the platform health probe.

    Returns 503 (not a stack trace) when the database is unreachable.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any DB failure means "unhealthy"
        return JsonResponse({"status": "error", "db": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})


def csrf_failure(request, reason=""):
    """Short 403 instead of Django's explanatory CSRF page; JSON for the
    assistant's endpoints so the widget can show it."""
    from django.shortcuts import render

    if request.path.startswith("/assistant/"):
        return JsonResponse({"error": "Your session expired. Reload the page."}, status=403)
    return render(request, "403.html", status=403)
