"""Authentication credential (PIN) validation.

The authentication credential for this project is a **6-digit numeric PIN**,
not a free-form password. This module is the single source of truth for that
rule: it is registered in ``AUTH_PASSWORD_VALIDATORS`` so Django's
``validate_password()`` enforces it everywhere a credential is set, and it is
also importable directly (``validate_pin``) for explicit checks.

The PIN itself is never stored in plaintext — callers still hand it to
``User.set_password()`` so Django's configured password hasher applies.
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# Exactly six ASCII digits, nothing else (no spaces, signs, or separators).
PIN_REGEX = re.compile(r"^\d{6}$")

PIN_LENGTH = 6

# Reused so the API, the Django validator, and tests all speak with one voice.
PIN_ERROR_MESSAGE = _("PIN must be exactly 6 numeric digits.")


def validate_pin(value):
    """Raise ``ValidationError`` unless ``value`` is exactly six digits.

    Rejects wrong length, non-numeric characters, empty values, and any
    embedded whitespace or sign characters.
    """
    if value is None or not PIN_REGEX.fullmatch(str(value)):
        raise ValidationError(PIN_ERROR_MESSAGE, code="invalid_pin")


class SixDigitPINValidator:
    """``AUTH_PASSWORD_VALIDATORS``-compatible wrapper around :func:`validate_pin`.

    Registering this makes ``django.contrib.auth.password_validation``'s
    ``validate_password()`` enforce the PIN policy, so every credential-setting
    path (registration, PIN reset, PIN change) shares the same rule.
    """

    def validate(self, password, user=None):
        validate_pin(password)

    def get_help_text(self):
        return PIN_ERROR_MESSAGE
