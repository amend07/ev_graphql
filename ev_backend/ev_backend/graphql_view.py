"""Hardened GraphQL view (Sprint 5, Parts 5 & 8).

Adds query-depth/complexity validation, introspection gating, an endpoint-wide
rate limit and safe error formatting on top of the file-upload view. Business
errors (our intentional, clean messages) pass through unchanged so the Flutter
client keeps seeing the same messages; database errors and internal bug types are
masked to a generic message and logged server-side, so SQL and implementation
details never reach clients.
"""

import logging

from django.conf import settings
from django.db import Error as DatabaseError
from graphene_file_upload.django import FileUploadGraphQLView

from accounts import ratelimit

from .errors import APIError
from .graphql_validation import (
    complexity_limit_validator,
    depth_limit_validator,
    introspection_validator,
)

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
    # Applied by graphene-django before execution. Each rule reads its limit from
    # settings at validation time, so override_settings tunes them per-test.
    validation_rules = [
        depth_limit_validator(),
        complexity_limit_validator(),
        introspection_validator(),
    ]

    def get_response(self, request, data, show_graphiql=False):
        """Throttle the endpoint before executing (per-IP, and per-user when
        authenticated), then defer to the base view.

        Introspection-in-dev (GraphiQL) is exempt: DEBUG turns the limit off so
        the explorer's repeated schema/query traffic is never counted. Health
        checks live on separate URLs and never reach this view. On exceeding the
        limit we return a clean GraphQL error with a 429 status — no internals.
        """
        limited = self._rate_limit_error(request)
        if limited is not None:
            return self.json_encode(request, {"errors": [limited]}), 429
        return super().get_response(request, data, show_graphiql)

    def _rate_limit_error(self, request):
        """Return a formatted GraphQL error dict if the caller is over the limit,
        else ``None``. Never raises — a limiter fault must not 500 the endpoint."""
        if settings.DEBUG:
            return None  # dev/GraphiQL introspection is not throttled
        cfg = getattr(settings, "GRAPHQL_RATELIMIT", None)
        if not cfg or not cfg.get("limit"):
            return None
        try:
            ratelimit.enforce(
                "GRAPHQL", ratelimit.get_client_ip(request), "ip", config=cfg
            )
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                ratelimit.enforce("GRAPHQL", str(user.pk), "account", config=cfg)
        except ratelimit.RateLimitExceeded as exc:
            logger.warning("GraphQL rate limit hit for %s", ratelimit.get_client_ip(request))
            return {
                "message": "Too many requests. Please slow down and try again later.",
                "extensions": {"code": "rate_limited", "retryAfter": exc.retry_after},
            }
        return None

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
