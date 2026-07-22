"""Input/business-rule validation for stations and reviews (Sprint 4).

Raising :class:`InvalidInput` surfaces a clean GraphQL error message and keeps
invalid data from ever reaching the model layer. Bounds are configurable via
settings so limits can be tuned without code changes.
"""

from django.conf import settings


class InvalidInput(Exception):
    """Raised when GraphQL input violates a validation or business rule."""


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def validate_name(value):
    if _blank(value):
        raise InvalidInput("Station name is required.")
    if len(value) > 100:
        raise InvalidInput("Station name must be at most 100 characters.")


def validate_location(value):
    if _blank(value):
        raise InvalidInput("Location is required.")
    if len(value) > 255:
        raise InvalidInput("Location must be at most 255 characters.")


def validate_description(value):
    if value and len(value) > settings.STATION_MAX_DESCRIPTION_LENGTH:
        raise InvalidInput(
            f"Description must be at most {settings.STATION_MAX_DESCRIPTION_LENGTH} characters."
        )


def validate_latitude(value):
    if value is None or not (-90.0 <= float(value) <= 90.0):
        raise InvalidInput("Latitude must be between -90 and 90.")


def validate_longitude(value):
    if value is None or not (-180.0 <= float(value) <= 180.0):
        raise InvalidInput("Longitude must be between -180 and 180.")


# Aliases the clients or legacy data may send, mapped onto the canonical codes.
# The mechanical part ("Type 2" -> "type2", "GB/T" -> "gb_t") is handled by
# lower-casing and stripping separators; this table only covers names that are
# not a spacing/case variant of the code itself.
_CHARGER_ALIASES = {
    'ccs1': 'ccs', 'combo': 'ccs', 'combo1': 'ccs',
    'combo2': 'ccs2',
    'j1772': 'type1', 'sae': 'type1',
    'mennekes': 'type2', 'iec62196': 'type2',
    'gbt': 'gb_t',
    'tesla': 'nacs', 'supercharger': 'nacs',
}


def normalize_charger_type(value):
    """Fold a connector string onto its canonical code, or return it unchanged if
    unrecognised (so validation, not normalisation, is what rejects it)."""
    if not value or not isinstance(value, str):
        return value
    key = value.strip().lower().replace(' ', '').replace('/', '_').replace('-', '_')
    return _CHARGER_ALIASES.get(key, key)


def normalize_charge_mode(value):
    """Fold an AC/DC string onto its canonical code (``ac``/``dc``)."""
    if not value or not isinstance(value, str):
        return value
    return value.strip().lower()


def _charger_type_codes():
    from .models import Station
    return {code for code, _ in Station.CHARGER_TYPE_CHOICES}


def _charge_mode_codes():
    from .models import Station
    return {code for code, _ in Station.CHARGE_MODE_CHOICES}


def validate_charger_type(value):
    if _blank(value):
        return  # optional; only validated when provided
    normalized = normalize_charger_type(value)
    allowed = _charger_type_codes()
    if normalized not in allowed:
        raise InvalidInput(
            f"Invalid charger type. Allowed: {', '.join(sorted(allowed))}."
        )


def validate_charge_mode(value):
    if _blank(value):
        return  # optional; model default (AC) applies
    normalized = normalize_charge_mode(value)
    allowed = _charge_mode_codes()
    if normalized not in allowed:
        raise InvalidInput(
            f"Invalid charge mode. Allowed: {', '.join(sorted(allowed))}."
        )


def validate_price(value):
    if value is None:
        return
    value = float(value)
    if value < 0:
        raise InvalidInput("Price per kWh cannot be negative.")
    if value > settings.STATION_MAX_PRICE_PER_KWH:
        raise InvalidInput("Price per kWh exceeds the maximum allowed.")


def validate_power(value):
    if value is None:
        return
    value = float(value)
    if value <= 0:
        raise InvalidInput("Power output must be positive.")
    if value > settings.STATION_MAX_POWER_KW:
        raise InvalidInput("Power output exceeds the maximum allowed.")


def validate_charger_count(value, field="Charger count"):
    if value is None:
        return
    value = int(value)
    if value < 1:
        raise InvalidInput(f"{field} must be at least 1.")
    if value > settings.STATION_MAX_CHARGERS:
        raise InvalidInput(f"{field} exceeds the maximum allowed.")


def validate_station_input(data, *, partial=False):
    """Validate a create (``partial=False``) or update (``partial=True``) payload.

    ``data`` maps field name → provided value. On create, required fields must
    be present and valid; on update only provided (non-None) fields are checked.
    """
    def has(key):
        return data.get(key) is not None

    if not partial or has("name"):
        validate_name(data.get("name"))
    if not partial or has("location"):
        validate_location(data.get("location"))
    if not partial or has("latitude"):
        validate_latitude(data.get("latitude"))
    if not partial or has("longitude"):
        validate_longitude(data.get("longitude"))

    if not partial and not has("price_per_kwh"):
        raise InvalidInput("Price per kWh is required.")
    if has("price_per_kwh"):
        validate_price(data.get("price_per_kwh"))

    if has("description"):
        validate_description(data["description"])
    if has("charger_type"):
        validate_charger_type(data["charger_type"])
    if has("charge_mode"):
        validate_charge_mode(data["charge_mode"])
    if has("power_output_kw"):
        validate_power(data["power_output_kw"])
    if has("num_of_charger"):
        validate_charger_count(data["num_of_charger"], "Number of chargers")
    if has("station_count"):
        validate_charger_count(data["station_count"], "Station count")


def validate_rating(value):
    lo, hi = settings.REVIEW_MIN_RATING, settings.REVIEW_MAX_RATING
    if value is None or not (lo <= int(value) <= hi):
        raise InvalidInput(f"Rating must be between {lo} and {hi}.")


def validate_comment(value):
    if value and len(value) > settings.REVIEW_MAX_COMMENT_LENGTH:
        raise InvalidInput(
            f"Comment must be at most {settings.REVIEW_MAX_COMMENT_LENGTH} characters."
        )
