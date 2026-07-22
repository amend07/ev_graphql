"""Signup email verification (Sprint W8).

Proving control of an EMAIL address at signup, and nothing else. What that proof
is then spent on — creating the phone-first account — belongs to
``services.create_phone_account``. Kept apart for the same reason
``phone_service`` is kept apart from ``identity``: "you hold this mailbox" is one
fact with one set of security properties; "and therefore this account is yours"
is a decision with several.

This is the EMAIL analogue of ``phone_service`` (SMS). It exists because the
platform has no SMS provider (see sms.py), so a phone-first signup proves the
person via the email they supply alongside the number. The consequence is
recorded wherever it matters: an account created this way has a PROVEN email and
a CLAIMED-not-proven phone. See docs/IDENTITY_ARCHITECTURE.md §9.2 and
``EmailVerification``.

Mirrors ``phone_service`` and ``services`` in its hardening because that design
is already right: rate-limited per IP and per identifier, constant-time
verification, attempt-limited, single-use.

THE ENUMERATION RULE: ``send_signup_otp`` returns the SAME message whether or not
the address is already registered. Signup does eventually have to tell the caller
"that email is taken" — but it says so at ``create_phone_account``, after a code
was proven, not from an unauthenticated send that would otherwise be a free
"is this email registered here" oracle.
"""

import logging

from django.contrib.auth import get_user_model

from ev_backend.errors import ValidationError

from . import ratelimit
from .auth_logging import log_event
from .models import EmailVerification

logger = logging.getLogger('accounts.email')

User = get_user_model()

#: The one reply every send path gives. See THE ENUMERATION RULE above.
GENERIC_CODE_SENT = "If that address can receive mail, a code has been sent."
GENERIC_CODE_INVALID = "Invalid or expired code."


def send_signup_otp(email, request=None):
    """Email a signup verification code. Returns the generic message.

    Rate-limited per IP and per address, with a per-address cooldown. Delivery
    failure is logged server-side (never with the code) and rolls the row back so
    a cooldown is not left behind for a code that reached nobody — the same
    contract ``phone_service.send_phone_otp`` keeps for SMS.
    """
    email = (email or '').strip()
    if not email or '@' not in email:
        # A malformed address is the caller's typo, not an enumeration signal;
        # refusing to say so would be user-hostile for no security gain.
        raise ValidationError("Enter a valid email address.")

    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce('OTP_REQUEST', ip, 'ip')
    ratelimit.enforce('OTP_REQUEST', email.lower(), 'account')

    ok, _wait = EmailVerification.can_request(email)
    if not ok:
        # Same reply as success: the cooldown must not be observable.
        log_event('signup_otp_requested', request=request, result='cooldown')
        return GENERIC_CODE_SENT

    try:
        EmailVerification.generate_for_email(email)
    except Exception:
        # Delivery failed: the row was rolled back inside generate_for_email's
        # caller contract only if we do it here — generate_for_email persists
        # then sends, so on failure the just-created row must go.
        EmailVerification.objects.filter(email__iexact=email).delete()
        log_event('signup_otp_send_failed', request=request, level=logging.ERROR)
        # Stay generic to the caller: a delivery outage is our problem, and the
        # reply is the same one every path gives.
        return GENERIC_CODE_SENT

    log_event('signup_otp_requested', request=request, result='sent')
    return GENERIC_CODE_SENT


def verify_signup_otp(email, code, request=None):
    """Check a signup code. Returns the normalised email on success.

    Raises ValidationError on any failure, with one message for every cause —
    "wrong code", "expired" and "never requested" are the same to the caller, who
    does the same thing in all three: ask for another one.
    """
    email = (email or '').strip()
    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce('OTP_VERIFY', ip, 'ip')
    ratelimit.enforce('OTP_VERIFY', email.lower(), 'account')

    verification = (
        EmailVerification.objects.filter(email__iexact=email)
        .order_by('-created_at')
        .first()
    )
    if verification is None:
        log_event('signup_otp_verify', request=request, result='no_code')
        raise ValidationError(GENERIC_CODE_INVALID)

    result = verification.verify(code)
    if result != 'ok':
        # `result` is logged for the operator, never returned to the caller.
        log_event('signup_otp_verify', request=request, result=result)
        raise ValidationError(GENERIC_CODE_INVALID)

    log_event('signup_otp_verify', request=request, result='ok')
    return email
