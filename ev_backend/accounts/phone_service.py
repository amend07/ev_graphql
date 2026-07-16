"""Phone verification (Sprint W7).

Proving control of a handset, and nothing else. What that proof is then SPENT on
— signing in, linking a number to an account, changing one — belongs to
identity.py. Kept apart because "you hold this phone" is one fact with one set of
security properties, while "and therefore you are this user" is a decision with
several, and fusing them is how an OTP minted for a link becomes a sign-in.

Mirrors ``services.py`` (email OTP) in its hardening because that design is
already right: rate-limited per IP and per identifier, constant-time verification,
attempt-limited, single-use, and enumeration-safe replies.

THE ENUMERATION RULE, which is why several branches here look redundant: every
outcome of ``send_phone_otp`` returns the SAME message. Whether the number is on
an account, is in cooldown, or has never been seen, the caller is told the same
thing. Anything else turns this into a "does this person have an account here"
oracle for the price of one request — and unlike an email address, a phone number
space is small and dense enough to walk.
"""

import logging

from django.utils import timezone

from ev_backend.errors import ValidationError

from . import ratelimit
from .auth_logging import log_event
from .models import PhoneVerification
from .phone import mask_phone, normalize_phone
from .sms import get_sms_backend

logger = logging.getLogger('accounts.phone')

#: The one reply every send path gives. See THE ENUMERATION RULE above.
GENERIC_CODE_SENT = "If that number can receive messages, a code has been sent."
GENERIC_CODE_INVALID = "Invalid or expired code."

#: Body of the message. Deliberately says what the code is for: a bare number is
#: indistinguishable from a phishing text, and users are entitled to know which
#: action they are approving before they type it anywhere.
OTP_MESSAGE = "{code} is your EV Charge Hub verification code. It expires in {minutes} minutes. We will never ask you for it."


def send_phone_otp(raw_phone, request=None):
    """Send a verification code. Returns ``(normalized_e164, message)``.

    Raises ValidationError for an unparseable number — that is the caller's typo,
    not an enumeration signal, and refusing to say "that is not a phone number"
    would be user-hostile for no security gain.

    Raises SmsUnavailable when no provider is configured. That propagates on
    purpose: it is the honest failure, typed so the client can say "phone sign-in
    is not available yet" instead of leaving someone waiting for a code that was
    never sent.
    """
    phone = normalize_phone(raw_phone)

    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce('OTP_REQUEST', ip, 'ip')
    # Rate-limited on the NORMALIZED number, which is the entire reason phone.py
    # exists. Limiting on the raw string would let `0911...`, `+251911...` and
    # `251-911-...` each spend their own budget against one handset.
    ratelimit.enforce('OTP_REQUEST', phone, 'account')

    ok, _wait = PhoneVerification.can_request(phone)
    if not ok:
        # Same reply as success: the cooldown must not be observable, or it
        # answers "was a code recently requested for this number".
        log_event('phone_otp_requested', request=request, result='cooldown')
        return phone, GENERIC_CODE_SENT

    verification, code = PhoneVerification.generate_for_phone(phone)
    try:
        get_sms_backend().send(
            to_e164=phone,
            body=OTP_MESSAGE.format(code=code, minutes=PhoneVerification.validity_minutes()),
        )
    except Exception:
        # Delivery failed, so the row must not survive: leaving it would start a
        # cooldown for a code that reached nobody, locking the user out of
        # retrying for a minute because OUR provider broke.
        verification.delete()
        # No `code` in the log payload — auth_logging scrubs known-sensitive
        # keys, and the way to not leak a credential is to not pass it.
        log_event('phone_otp_send_failed', request=request, level=logging.ERROR)
        raise

    log_event('phone_otp_requested', request=request, result='sent')
    return phone, GENERIC_CODE_SENT


def verify_phone_otp(raw_phone, code, request=None):
    """Check a code. Returns the normalized E.164 on success.

    Raises ValidationError on any failure, with one message for every cause. The
    distinction between "wrong code", "expired code" and "no code was ever
    requested" is useful to an attacker and useless to a user, who does the same
    thing in all three cases: ask for another one.
    """
    phone = normalize_phone(raw_phone)

    ip = ratelimit.get_client_ip(request)
    ratelimit.enforce('OTP_VERIFY', ip, 'ip')
    ratelimit.enforce('OTP_VERIFY', phone, 'account')

    verification = (
        PhoneVerification.objects.filter(phone_e164=phone).order_by('-created_at').first()
    )
    if verification is None:
        log_event('phone_otp_verify', request=request, result='no_code')
        raise ValidationError(GENERIC_CODE_INVALID)

    result = verification.verify(code)
    if result != 'ok':
        # `result` is logged, never returned: the operator needs to tell locked
        # from invalid when reading an incident; the caller does not get to.
        log_event('phone_otp_verify', request=request, result=result)
        raise ValidationError(GENERIC_CODE_INVALID)

    log_event('phone_otp_verify', request=request, result='ok')
    return phone


def describe_destination(e164):
    """Where a code went, for echoing back on the code-entry screen.

    Masked. The screen has to confirm the number to catch a typo, and the last
    four digits do that; the whole number would make any shoulder-surf or
    screenshot a disclosure of an identifier the user did not choose to show.
    """
    return mask_phone(e164)


def touch_identity_use(identity):
    """Record that an identity was just used to sign in."""
    identity.last_used_at = timezone.now()
    identity.save(update_fields=['last_used_at'])
