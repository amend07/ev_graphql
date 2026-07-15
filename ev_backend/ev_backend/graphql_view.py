"""Hardened GraphQL view (Sprint 5, Parts 5 & 8).

Adds query-depth validation and safe error formatting on top of the file-upload
view. Business errors (our intentional, clean messages) pass through unchanged so
the Flutter client keeps seeing the same messages; database errors and internal
bug types are masked to a generic message and logged server-side, so SQL and
implementation details never reach clients.
"""

import logging

from django.db import Error as DatabaseError
from graphene_file_upload.django import FileUploadGraphQLView

from .errors import APIError
from .graphql_validation import depth_limit_validator

logger = logging.getLogger("ev_backend.graphql")

# Exception types that indicate an internal fault (SQL/text or a code bug) and
# must never have their message returned to a client.
_MASK_TYPES = (
    DatabaseError,
    AttributeError,
    TypeError,
    KeyError,
    IndexError,
    NotImplementedError,
)

_GENERIC_MESSAGE = "An internal error occurred. Please try again later."


class HardenedGraphQLView(FileUploadGraphQLView):
    # Applied by graphene-django before execution.
    validation_rules = [depth_limit_validator()]

    @staticmethod
    def format_error(error):
        original = getattr(error, "original_error", None)
        if original is not None and isinstance(original, _MASK_TYPES):
            logger.error("Masked GraphQL error: %r", original)
            return {"message": _GENERIC_MESSAGE}

        formatted = FileUploadGraphQLView.format_error(error)

        # Attach the stable code for typed failures (B1). Purely additive: the
        # message is untouched, so clients that read `errors[0].message` — every
        # current one — are unaffected, while new callers can branch on the code
        # instead of matching prose.
        if isinstance(original, APIError):
            extensions = dict(formatted.get("extensions") or {})
            extensions["code"] = original.code
            formatted["extensions"] = extensions

        return formatted
