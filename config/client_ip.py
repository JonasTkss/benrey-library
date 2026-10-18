"""Which IP address a request came from, for the login lockout (django-axes).

On Railway every request reaches the app from the platform's proxy
(100.64.x.x), so REMOTE_ADDR is the same for all visitors. Railway's edge puts
the visitor's address in X-Real-IP. That header is only trusted when
TRUST_X_REAL_IP is on (production), because without a proxy in front anyone
could send it and pick their own "IP" to dodge the lockout.
"""

import ipaddress

from django.conf import settings


def client_ip(request) -> str | None:
    if getattr(settings, "TRUST_X_REAL_IP", False):
        candidate = request.META.get("HTTP_X_REAL_IP", "").strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass  # missing or malformed: fall back to the socket address
    return request.META.get("REMOTE_ADDR")
