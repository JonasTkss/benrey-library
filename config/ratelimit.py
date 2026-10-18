"""Per-IP request limits, in front of every view.

Complements, not replaces, the more specific protections:
- django-axes locks out repeated failed logins (username+IP, and IP alone),
- the assistant has its own per-user limits (assistant/views.py).

This layer stops a single client from hammering any endpoint: scraping admin
pages, guessing cover URLs, or flooding the login form with requests that
are not even valid attempts. Static files never reach it (WhiteNoise answers
them first), and the health check is exempt so the platform probe is never
throttled.

Counters for login and the assistant live in the shared DB cache, so all
gunicorn workers count together. The broad "everything" limit uses a fast
per-process memory cache: with 2 workers it is effectively 2x looser, which
is fine for a coarse anti-scraping limit and avoids a DB write on every page
view. At more traffic, all of this would move to Redis.
"""

import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import caches
from django.http import HttpResponse, JsonResponse

from config.client_ip import client_ip


@dataclass(frozen=True)
class Rule:
    name: str
    limit: int  # requests
    window: int  # seconds
    cache: str  # cache alias


def _login_path():
    return "/" + settings.ADMIN_URL + "login/"


def rule_for(request) -> Rule | None:
    path = request.path
    if path == "/healthz":
        return None
    if request.method == "POST" and path == _login_path():
        return Rule("login", settings.RATELIMIT_LOGIN_PER_MINUTE, 60, "default")
    if path.startswith("/assistant/"):
        return Rule("assistant", settings.RATELIMIT_ASSISTANT_PER_MINUTE, 60, "default")
    if path.startswith("/covers/"):
        return Rule("covers", settings.RATELIMIT_COVERS_PER_MINUTE, 60, "ratelimit")
    return Rule("all", settings.RATELIMIT_PER_MINUTE, 60, "ratelimit")


def over_limit(rule: Rule, ip: str) -> bool:
    cache = caches[rule.cache]
    bucket = int(time.time()) // rule.window
    key = f"rl:{rule.name}:{ip}:{bucket}"
    cache.add(key, 0, rule.window)
    try:
        return cache.incr(key) > rule.limit
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, rule.window)
        return False


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rule = rule_for(request)
        if rule and getattr(settings, "RATELIMIT_ENABLED", True):
            ip = client_ip(request) or "unknown"
            if over_limit(rule, ip):
                return too_many(request, rule)
        return self.get_response(request)


def too_many(request, rule):
    message = "Too many requests. Please wait a minute and try again."
    if request.path.startswith("/assistant/"):
        response = JsonResponse({"error": message}, status=429)
    else:
        response = HttpResponse(message, status=429, content_type="text/plain")
    response["Retry-After"] = str(rule.window)
    return response


class SecurityHeadersMiddleware:
    """Headers Django does not set itself: switch off browser features the
    admin never uses, so injected content could not use them either."""

    POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Permissions-Policy", self.POLICY)
        return response
