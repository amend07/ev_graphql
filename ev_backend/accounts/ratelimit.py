"""Brute-force protection for authentication endpoints (Part 3).

Cache-backed counters enforce per-IP and per-account limits with a temporary
lockout once a threshold is exceeded. Thresholds are configurable via the
``AUTH_RATELIMIT`` setting. With the default local-memory cache the counters are
per-process; configure a shared cache (e.g. Redis via ``REDIS_URL``) in
production so limits hold across workers.
"""

from django.conf import settings
from django.core.cache import cache

# Safe defaults; overridden per-scope by settings.AUTH_RATELIMIT.
# window/lockout are in seconds.
DEFAULTS = {
    "LOGIN": {"limit": 10, "window": 300, "lockout": 900},
    "OTP_REQUEST": {"limit": 5, "window": 3600, "lockout": 3600},
    "OTP_VERIFY": {"limit": 10, "window": 900, "lockout": 900},
}


class RateLimitExceeded(Exception):
    """Raised when an identifier is over its limit or currently locked out."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("Too many attempts. Please try again later.")


def _config(scope):
    base = dict(DEFAULTS.get(scope, {"limit": 10, "window": 300, "lockout": 900}))
    base.update(getattr(settings, "AUTH_RATELIMIT", {}).get(scope, {}))
    return base


def get_client_ip(request):
    """Best-effort client IP, honouring a single proxy hop via X-Forwarded-For."""
    if request is None:
        return "unknown"
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _key(scope, kind, identifier):
    return f"authrl:{scope}:{kind}:{identifier}"


def enforce(scope, identifier, kind="ip"):
    """Count one attempt for ``(scope, kind, identifier)``.

    Raises :class:`RateLimitExceeded` if the identifier is locked out or exceeds
    the configured limit within the window. On exceeding the limit a lockout key
    is set for ``lockout`` seconds.
    """
    if not identifier:
        return
    cfg = _config(scope)
    base = _key(scope, kind, identifier)
    lock_key = base + ":lock"

    if cache.get(lock_key):
        raise RateLimitExceeded(cfg["lockout"])

    try:
        count = cache.incr(base)
    except ValueError:
        # First hit in this window; seed the counter with its TTL.
        cache.set(base, 1, cfg["window"])
        count = 1

    if count > cfg["limit"]:
        cache.set(lock_key, True, cfg["lockout"])
        cache.delete(base)
        raise RateLimitExceeded(cfg["lockout"])


def reset(scope, identifier, kind="ip"):
    """Clear counters/lockout for an identifier (e.g. after a success)."""
    base = _key(scope, kind, identifier)
    cache.delete(base)
    cache.delete(base + ":lock")
