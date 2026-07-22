"""Authentication service layer.

Holds the credential business logic once so the canonical PIN mutations and the
backward-compatible legacy (``password``-named) aliases share a single
implementation. Handlers in ``schema.py`` stay thin.

Security properties enforced here: 6-digit PIN policy, hashed credentials
(never plaintext), rate limiting, generic anti-enumeration responses,
constant-time OTP handling, and structured audit logging.
"""

import hashlib
import logging

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import ratelimit
from .auth_logging import log_event
from .models import AuthIdentity, PasswordResetOTP
from .phone import normalize_phone

User = get_user_model()

# Generic, enumeration-safe user-facing messages.
GENERIC_OTP_SENT = "If the email is registered, a reset code has been sent."
GENERIC_OTP_INVALID = "Invalid or expired code."
GENERIC_RATE_LIMITED = "Too many attempts. Please try again later."
# One message for "unknown number" and "wrong PIN" alike (W8), matching the
# wording tokenAuth already returns so the two login paths are indistinguishable.
GENERIC_INVALID_CREDENTIALS = "Please enter valid credentials"

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
    # Station owners register as PENDING and are approved by an admin (B1 Phase
    # 2). They stay `is_active=True` and can sign in and use the app as a
    # customer immediately — approval gates station management, not access. Any
    # other role has no approval state, so the field stays NULL.
    owner_status = User.OWNER_PENDING if is_station_owner else None
    user = User.objects.create_user(
        username=username, email=email, password=pin, role=role,
        owner_status=owner_status,
    )
    log_event("account_created", request=request, user=user, role=role)
    _notify_admins_of_new_owner(user, is_station_owner)
    return user


def _notify_admins_of_new_owner(user, is_station_owner):
    """A new station-owner signup lands in the admin approval queue, so tell the
    admins. Imported locally: `accounts` loads before `notifications`, so a
    module-level import could hit the app registry too early."""
    if not is_station_owner:
        return
    from notifications.models import Notification
    from notifications.service import notify_admins

    notify_admins(
        notification_type=Notification.TYPE_STATION,
        title="New station owner awaiting approval",
        body=f"{user.username} signed up as a station owner and needs review.",
        related_id=user.id,
    )


def _internal_username_for_phone(phone_e164):
    """A stable, internal, non-guessable username for a phone-first account.

    Username stays required by ``AbstractUser`` but is no longer an identity a
    person types — phone is (W8). It is derived from the number rather than
    random so it is stable and debuggable, and HASHED rather than raw so the
    number never lands in a field ``usersPage(search:)`` matches on. Same reasoning
    as ``identity._generate_username``; kept separate because this account is not
    created through the provider-identity path.
    """
    digest = hashlib.sha256(f'phone:{phone_e164}'.encode()).hexdigest()[:16]
    return f'phone_{digest}'


def validate_new_phone_account(phone, email, pin):
    """Validate a prospective phone+PIN signup WITHOUT creating anything.

    Returns the normalised E.164 so a caller need not normalise twice. Split out
    so ``registerWithPhone`` can run it BEFORE spending the single-use email OTP:
    a weak PIN or an already-registered number must not cost the caller their
    code — the same rule ``reset_pin`` keeps when it validates the new PIN before
    burning a correct OTP.

    Uniqueness is ultimately a DB guarantee (``phone_e164`` is UNIQUE); these
    checks exist to return a specific message instead of a caught IntegrityError
    that cannot say which column collided.
    """
    e164 = normalize_phone(phone)  # raises ValidationError on an unparseable number

    try:
        validate_password(pin)
    except ValidationError as e:
        raise CredentialError("; ".join(e.messages))

    if User.objects.filter(phone_e164=e164).exists():
        raise CredentialError("That phone number is already registered.")
    if User.objects.filter(email__iexact=email).exists():
        raise CredentialError("Email already registered")
    return e164


@transaction.atomic
def create_phone_account(phone, email, pin, is_station_owner=False, request=None):
    """Register a phone-first account (W8). Phone is the canonical identity.

    Called only after ``email_service.verify_signup_otp`` has proven the email,
    so the account starts with a PROVEN email and a CLAIMED-not-proven phone: no
    SMS provider exists to prove the handset (sms.py), so ``phone_verified_at``
    stays NULL and no verified ``phone`` identity is created. The phone is still
    canonical — it is UNIQUE and it is the login key — it is simply not marked
    proven, because it was not. See docs/IDENTITY_ARCHITECTURE.md §9.2.

    The credential is a 6-digit PIN, hashed, exposed to the identity system as a
    ``password`` provider exactly as the legacy login is (migration 0009), so the
    last-identity and unlink rules treat a phone+PIN account uniformly.
    """
    e164 = validate_new_phone_account(phone, email, pin)

    role = "station_owner" if is_station_owner else "user"
    owner_status = User.OWNER_PENDING if is_station_owner else None

    username = _internal_username_for_phone(e164)
    user = User.objects.create_user(
        username=username,
        email=email,
        password=pin,          # hashed by create_user; never plaintext
        role=role,
        owner_status=owner_status,
        phone_e164=e164,       # canonical, but unproven — see docstring
        phone_verified_at=None,
    )

    # The PIN login, modelled as a provider so a phone+PIN account is one identity
    # among several (mirrors migration 0009 for legacy accounts). verified_at is
    # NULL: the OTP proved the email, not that this username belongs to a human.
    AuthIdentity.objects.create(
        user=user,
        provider=AuthIdentity.PROVIDER_PASSWORD,
        subject=username,
        verified_at=None,
    )

    log_event("account_created", request=request, user=user, role=role, method="phone_pin")
    _notify_admins_of_new_owner(user, is_station_owner)
    return user


def authenticate_phone_pin(phone, pin, request=None):
    """Return the user for a valid phone + PIN, else raise (W8).

    The primary sign-in path: phone is the canonical identity, the PIN is the
    credential. Raises ``CredentialError`` for any authentication failure and
    ``ratelimit.RateLimitExceeded`` when throttled — the caller translates both,
    exactly as ``signInWithPhone`` does.

    Generic and timing-equalised: an unknown number and a wrong PIN return the
    same message and take the same time (a dummy hash comparison runs when there
    is no user), so this is not an oracle for "is this number registered". A phone
    space is small and dense enough to walk, which is why the equalisation matters
    more here than for email. Rate-limited per IP and per NORMALISED number, so
    the three ways to write one number cannot each spend their own budget.
    """
    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce("LOGIN", ip, "ip")

    try:
        e164 = normalize_phone(phone)
    except Exception:
        # Unparseable: cannot match an account. Do not distinguish it from a wrong
        # credential, but still burn time so the endpoint is not a parser oracle.
        check_password(str(pin), _DUMMY_OTP_HASH)
        log_event("login_failure", request=request, method="phone_pin")
        raise CredentialError(GENERIC_INVALID_CREDENTIALS)

    ratelimit.enforce("LOGIN", e164, "account")

    user = User.objects.filter(phone_e164=e164).first()
    if user is None or not user.check_password(pin):
        if user is None:
            check_password(str(pin), _DUMMY_OTP_HASH)  # equalise timing
        log_event("login_failure", request=request, method="phone_pin")
        raise CredentialError(GENERIC_INVALID_CREDENTIALS)

    ratelimit.reset("LOGIN", ip, "ip")
    ratelimit.reset("LOGIN", e164, "account")
    log_event("login_success", request=request, user=user, method="phone_pin")
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
