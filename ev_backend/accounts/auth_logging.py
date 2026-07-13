"""Structured authentication logging (Part 5).

Emits key=value audit lines on the ``accounts.auth`` logger. Credentials and
tokens are never logged: any field named like a secret is dropped before
formatting, so callers cannot accidentally leak a PIN, OTP, JWT, or refresh
token even if they pass one in.
"""

import logging

logger = logging.getLogger("accounts.auth")

# Field names that must never reach the log, regardless of caller.
_SENSITIVE = {
    "pin", "new_pin", "current_pin", "password", "new_password", "current_password",
    "otp", "code", "token", "jwt", "access_token", "refresh_token", "secret",
}


def _scrub(fields):
    return {
        k: v for k, v in fields.items()
        if k.lower() not in _SENSITIVE and v is not None
    }


def log_event(event, request=None, user=None, level=logging.INFO, **fields):
    """Log a single structured auth event.

    ``event`` is a stable machine-readable name (e.g. ``login_success``).
    ``user``/``request`` contribute ``user_id`` and ``ip``. Extra ``fields`` are
    appended as ``key=value`` after being scrubbed of anything sensitive.
    """
    # Imported lazily to avoid a circular import with ratelimit at module load.
    from .ratelimit import get_client_ip

    parts = [f"event={event}"]
    if user is not None and getattr(user, "id", None):
        parts.append(f"user_id={user.id}")
    if request is not None:
        parts.append(f"ip={get_client_ip(request)}")
    for key, value in _scrub(fields).items():
        parts.append(f"{key}={value}")

    logger.log(level, " ".join(parts))
