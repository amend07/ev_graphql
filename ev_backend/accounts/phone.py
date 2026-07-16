"""E.164 phone normalisation (W7).

One definition of "what is this phone number", used by every path that stores,
looks up or compares one. Two representations of the same number is how a
`UNIQUE` constraint silently stops enforcing "one human, one account":
`+251911223344` and `0911223344` are the same phone and would occupy two rows.

Deliberately NOT `phonenumbers` (libphonenumber): that library is ~10MB of
metadata and is worth it when you must validate carrier/line-type across every
region. We need normalise-and-compare for a platform operating in one country,
which is a parse and a prefix rule. If the platform goes multi-region, swap the
implementation behind `normalize_phone` — nothing else knows how it works.
"""

import re

from django.conf import settings

from ev_backend.errors import ValidationError

#: Default region for numbers entered without a country code. Ethiopia (+251).
#: Configurable because it is a business fact, not a physical constant.
DEFAULT_COUNTRY_CODE = getattr(settings, 'PHONE_DEFAULT_COUNTRY_CODE', '251')

#: E.164 allows at most 15 digits including the country code.
E164_MAX_DIGITS = 15
E164_MIN_DIGITS = 8

_NON_DIGITS = re.compile(r'[^\d+]')


def normalize_phone(raw: str, *, default_country: str | None = None) -> str:
    """Return `raw` as E.164 (`+CCXXXXXXXXX`), or raise ValidationError.

    Accepts the shapes a human actually types: `0911 22 33 44`,
    `+251-911-223344`, `251911223344`, `(0911) 223344`.

    A leading `0` is a *national trunk prefix*, not part of the number: it is
    stripped before the country code is applied. Getting this wrong is the
    classic bug — `+2510911223344` looks plausible and is a different, invalid
    number that would sit next to the real one in the table.
    """
    if not raw or not raw.strip():
        raise ValidationError("A phone number is required.")

    country = default_country or DEFAULT_COUNTRY_CODE
    cleaned = _NON_DIGITS.sub('', raw.strip())

    # A `+` is only meaningful at the front; `0+91...` is a typo, not a format.
    if '+' in cleaned[1:]:
        raise ValidationError("Enter a valid phone number.")

    if cleaned.startswith('+'):
        digits = cleaned[1:]
    elif cleaned.startswith('00'):
        # International access code — the older way of writing `+`.
        digits = cleaned[2:]
    elif cleaned.startswith('0'):
        # National format: drop the trunk prefix, prepend the country code.
        digits = country + cleaned.lstrip('0')
    elif cleaned.startswith(country):
        digits = cleaned
    else:
        # A bare subscriber number with no prefix of any kind.
        digits = country + cleaned

    if not digits.isdigit():
        raise ValidationError("Enter a valid phone number.")
    if not (E164_MIN_DIGITS <= len(digits) <= E164_MAX_DIGITS):
        raise ValidationError("Enter a valid phone number.")

    return f'+{digits}'


def mask_phone(e164: str) -> str:
    """`+251911223344` -> `+251•••••3344`.

    For anywhere a number is echoed back to a user: an OTP screen confirming
    where the code went, a linked-identities list. Showing the whole number turns
    any read of that screen into a disclosure, and the last four digits are
    enough for someone to recognise their own.
    """
    if len(e164) < 8:
        return e164
    return f'{e164[:4]}{"•" * (len(e164) - 8)}{e164[-4:]}'
