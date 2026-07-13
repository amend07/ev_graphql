"""Authentication service layer.

Holds the credential business logic once so the canonical PIN mutations and the
backward-compatible legacy (``password``-named) aliases share a single
implementation. Handlers in ``schema.py`` stay thin.

Security properties enforced here: 6-digit PIN policy, hashed credentials
(never plaintext), rate limiting, generic anti-enumeration responses,
constant-time OTP handling, and structured audit logging.
"""

import logging

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import ratelimit
from .auth_logging import log_event
from .models import PasswordResetOTP

User = get_user_model()

# Generic, enumeration-safe user-facing messages.
GENERIC_OTP_SENT = "If the email is registered, a reset code has been sent."
GENERIC_OTP_INVALID = "Invalid or expired code."
GENERIC_RATE_LIMITED = "Too many attempts. Please try again later."

# Pre-computed hash used to keep verification timing constant when there is no
# OTP (or user) for the supplied email — defeats timing-based enumeration.
_DUMMY_OTP_HASH = make_password("000000")


class CredentialError(Exception):
    """Raised for caller-visible validation problems (e.g. duplicate, weak PIN)."""


def create_account(username, email, pin, is_station_owner=False, request=None):
    """Register a user under the 6-digit PIN policy. Credential is hashed."""
    try:
        validate_password(pin)
    except ValidationError as e:
        raise CredentialError("; ".join(e.messages))

    if User.objects.filter(username=username).exists():
        raise CredentialError("Username already exists")
    if User.objects.filter(email__iexact=email).exists():
        raise CredentialError("Email already registered")

    role = "station_owner" if is_station_owner else "user"
    user = User.objects.create_user(
        username=username, email=email, password=pin, role=role
    )
    log_event("account_created", request=request, user=user, role=role)
    return user


def change_pin(user, current_pin, new_pin, request=None):
    """Authenticated PIN change. Returns ``(success, message)``."""
    if not user.check_password(current_pin):
        log_event("pin_change_failed", request=request, user=user, reason="wrong_current")
        return False, "Current PIN is incorrect."

    try:
        validate_password(new_pin, user)
    except ValidationError as e:
        return False, "; ".join(e.messages)

    user.set_password(new_pin)          # hashed, never plaintext
    user.save()
    _invalidate_other_sessions(user, request)
    log_event("pin_changed", request=request, user=user)
    return True, "PIN changed successfully."


def send_reset_otp(email, request=None):
    """Issue a reset OTP. Always returns the same generic message.

    Rate-limited per IP and per account, with a per-account cooldown between
    requests. The email existence is never disclosed.
    """
    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce("OTP_REQUEST", ip, "ip")
    ratelimit.enforce("OTP_REQUEST", email.lower(), "account")

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        log_event("pin_reset_requested", request=request, result="unknown_email")
        return GENERIC_OTP_SENT

    ok, _wait = PasswordResetOTP.can_request(user)
    if not ok:
        log_event("pin_reset_requested", request=request, user=user, result="cooldown")
        return GENERIC_OTP_SENT

    try:
        PasswordResetOTP.generate_for_user(user)
        log_event("pin_reset_requested", request=request, user=user, result="sent")
    except Exception:
        # Delivery failed: log server-side (no OTP value) but stay generic.
        log_event("otp_send_failed", request=request, user=user, level=logging.ERROR)
    return GENERIC_OTP_SENT


def reset_pin(email, otp, new_pin, request=None):
    """Verify an OTP and set a new PIN. Returns ``(success, message)``."""
    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce("OTP_VERIFY", ip, "ip")
    ratelimit.enforce("OTP_VERIFY", email.lower(), "account")

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    reset_obj = PasswordResetOTP.objects.filter(user=user).first() if user else None

    if user is None or reset_obj is None:
        # Equalise timing and give nothing away.
        check_password(str(otp), _DUMMY_OTP_HASH)
        log_event("otp_verify_failed", request=request, result="no_request")
        return False, GENERIC_OTP_INVALID

    # Validate the new PIN first so a correct OTP is not burned on a bad PIN.
    try:
        validate_password(new_pin, user)
    except ValidationError as e:
        return False, "; ".join(e.messages)

    outcome = reset_obj.verify(otp)
    if outcome == "ok":
        user.set_password(new_pin)      # hashed, never plaintext
        user.save()
        _invalidate_other_sessions(user, request)
        log_event("otp_verified", request=request, user=user)
        log_event("pin_reset", request=request, user=user)
        return True, "PIN reset successfully."

    if outcome == "expired":
        log_event("otp_expired", request=request, user=user)
    elif outcome == "locked":
        log_event("account_locked", request=request, user=user,
                  level=logging.WARNING, reason="otp_max_attempts")
    else:
        log_event("otp_verify_failed", request=request, user=user)
    return False, GENERIC_OTP_INVALID


def _invalidate_other_sessions(user, request=None):
    """Best-effort invalidation of a user's other auth after a PIN change/reset.

    Changing ``user.password`` rotates the value Django's session-auth hash is
    derived from, so other sessions stop authenticating. When an authenticated
    request is present we call ``update_session_auth_hash`` so the current caller
    stays signed in. If the long-running JWT refresh-token app is installed, its
    tokens are revoked so expired access tokens cannot be renewed. Already-issued
    stateless access tokens expire naturally (no blacklist — architecture
    unchanged).
    """
    if request is not None and getattr(request, "user", None) == user:
        try:
            update_session_auth_hash(request, user)
        except Exception:
            pass

    try:
        from graphql_jwt.refresh_token.models import RefreshToken

        RefreshToken.objects.filter(user=user, revoked__isnull=True).update(
            revoked=timezone.now()
        )
    except Exception:
        pass
