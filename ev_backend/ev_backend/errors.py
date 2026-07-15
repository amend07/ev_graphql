"""Typed API failures (Sprint B1, Phase 3/6).

Resolvers used to raise bare ``Exception("...")``. The message reached clients
intact, but nothing else did: callers had to string-match to tell "you may not do
that" from "that does not exist", and every new message risked breaking them.

These carry a stable machine-readable ``code`` alongside the same human message.
``HardenedGraphQLView`` surfaces the code in ``extensions.code``, which is an
additive change: ``errors[0].message`` is byte-for-byte what it was, so existing
mobile/web error handling is untouched, while new clients can branch on the code.

Raise these instead of ``Exception`` for every intentional, caller-visible
refusal. Anything else that escapes a resolver is a bug and is masked by the view.
"""


class APIError(Exception):
    """Base class for intentional, caller-visible failures."""

    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AuthenticationRequired(APIError):
    code = "unauthenticated"


class PermissionDenied(APIError):
    code = "forbidden"


class NotFound(APIError):
    code = "not_found"


class ValidationError(APIError):
    """Caller sent something the domain rejects (bad input, bad transition)."""

    code = "validation"


class Conflict(APIError):
    """The request is well-formed but conflicts with the system's current state.

    Used for the safety interlocks: deactivating yourself, deleting yourself,
    removing the last administrator.
    """

    code = "conflict"
