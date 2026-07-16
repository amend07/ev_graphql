"""Token revocation via `token_version` (Sprint W7 §5b).

THE PROBLEM THIS SOLVES: before W7 the platform could not revoke a session at
all. The JWT is stateless, `graphql_jwt.refresh_token` is not installed, and
`JWT_ALLOW_REFRESH` is on — so a leaked token was valid for its full lifetime
AND could be walked forward indefinitely by `refreshToken`. Changing a PIN did
not help. There was no answer to "my phone was stolen" other than waiting.

THE MECHANISM: every token carries the user's `token_version` as a `tv` claim.
`logoutEverywhere` increments the column, and every token minted before that
increment now disagrees with it and is refused. One integer, one comparison.

WHY THE DECODE HANDLER AND NOT `get_user_by_natural_key`: the obvious place to
assert this is where the user is loaded, but `graphql_jwt.utils.get_user_by_payload`
passes the natural-key handler a *username*, not the payload — the claim is not
visible from there. The decode handler is the one seam that sees the payload, and
it happens to be the correct one for a second reason: it is called by the auth
middleware, by `verifyToken`, AND by `refreshToken`. A check that missed
`refreshToken` would let a revoked token mint a fresh, valid one — revocation
that revokes nothing.

THE COST, STATED PLAINLY: one extra indexed lookup per authenticated request.
That is the honest price of revocation on stateless tokens and it is why the
value is `.only()`-loaded. The alternative — a deny-list of revoked tokens — needs
the same lookup plus storage that grows without bound.
"""

from django.contrib.auth import get_user_model
from graphql_jwt.exceptions import JSONWebTokenError
from graphql_jwt.utils import jwt_decode as _library_jwt_decode
from graphql_jwt.utils import jwt_payload as _library_jwt_payload

from ev_backend.errors import AuthenticationRequired

#: The claim name. Short because it rides on every request.
TOKEN_VERSION_CLAIM = 'tv'


class TokenRevoked(JSONWebTokenError, AuthenticationRequired):
    """This token belongs to a generation that has been revoked.

    Inherits from BOTH parents on purpose, and each one is load-bearing:

    * ``JSONWebTokenError`` — graphql_jwt's own machinery recognises it, so this
      travels the paths the library already has for a bad token rather than
      escaping as an unhandled exception.
    * ``AuthenticationRequired`` — makes ``HardenedGraphQLView.format_error``
      stamp ``extensions.code = 'unauthenticated'``.

    That code is the whole point. The Flutter client's ``_isAuthError``
    (cores/network/graphql_service.dart) matches on ``code == 'unauthenticated'``
    or on specific message prose. Raising a bare ``JSONWebTokenError`` would give
    it neither: the token would be refused by the server and the client would
    show an error and keep the dead token forever, never signing the user out —
    a revocation the victim cannot see.

    The tempting shortcut was to reuse ``JSONWebTokenExpired`` because its
    "Signature has expired" message is already matched. It is rejected: the token
    has NOT expired, and a message that says otherwise sends whoever debugs the
    stolen-phone report looking at clock skew. The code carries the meaning; the
    message is free to be true.
    """

    code = 'unauthenticated'
    default_message = 'Your session was ended. Please sign in again.'

    def __init__(self, message=None):
        AuthenticationRequired.__init__(self, message or self.default_message)


def jwt_payload(user, context=None):
    """The library payload plus this user's current `token_version`."""
    payload = _library_jwt_payload(user, context)
    payload[TOKEN_VERSION_CLAIM] = user.token_version
    return payload


def jwt_decode(token, context=None):
    """Decode as the library does, then refuse tokens from a revoked generation.

    Signature and expiry are verified first, by delegating: a token that fails
    those raises before we touch the database, so an attacker cannot use this
    path to probe for usernames with forged tokens.
    """
    payload = _library_jwt_decode(token, context)

    username = payload.get(get_user_model().USERNAME_FIELD)
    if not username:
        # No subject to check against. The library raises on this immediately
        # afterwards in get_user_by_payload; not our error to invent.
        return payload

    current = (
        get_user_model()
        .objects.filter(**{get_user_model().USERNAME_FIELD: username})
        .values_list('token_version', flat=True)
        .first()
    )
    if current is None:
        # Deleted user. The library's own lookup will fail next; returning the
        # payload keeps that its decision rather than duplicating it here.
        return payload

    # `.get(claim, 0)` is what keeps existing sessions alive across this deploy,
    # and it is a requirement, not a kindness: every token issued before W7 has
    # no `tv` claim, and treating a missing claim as invalid would sign out every
    # user on the platform the moment this ships. Absent means generation 0,
    # which matches the column default — so pre-W7 tokens keep working until
    # someone actually revokes, and after a revocation (tv >= 1) those same
    # legacy tokens stop working, which is exactly the intent.
    if payload.get(TOKEN_VERSION_CLAIM, 0) != current:
        raise TokenRevoked()

    return payload
