"""Resolver authorization (Sprint B1, Phase 1).

Every GraphQL field declares its policy with exactly one of these decorators —
including the deliberately public ones, via :func:`public`. Nothing is protected
by being hard to reach: before B1, `stationBookings` was owner-gated while the
same rows were readable anonymously through `stationById { bookings { user } }`.
Reachability is not authorization, so each field now says what it requires and
``test_authorization_policy`` fails if a new field forgets to.

The decorators refuse with typed errors (:mod:`ev_backend.errors`), so callers
can branch on ``extensions.code`` instead of matching message strings.
"""

from functools import wraps

from ev_backend.errors import AuthenticationRequired, PermissionDenied

# Policy markers. `test_authorization_policy` reads these off each resolver, so a
# field cannot ship without an explicit decision about who may call it.
POLICY_ATTR = "_auth_policy"

POLICY_PUBLIC = "public"
POLICY_AUTHENTICATED = "authenticated"
POLICY_ACTIVE = "active"
POLICY_OWNER = "station_owner"
POLICY_ADMIN = "admin"


def _mark(func, policy):
    setattr(func, POLICY_ATTR, policy)
    return func


def public(func):
    """Intentionally unauthenticated.

    A marker, not a no-op: it is the difference between "anyone may read this"
    and "nobody remembered to check". Only for data that is genuinely public —
    a station's public listing. Never for anything reaching a user's private
    fields or a booking.
    """
    return _mark(func, POLICY_PUBLIC)


def admin_required(func):
    @wraps(func)
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated:
            raise AuthenticationRequired("Authentication required.")
        if user.role != 'admin':
            raise PermissionDenied("Only admin can perform this action.")
        return func(self, info, *args, **kwargs)

    return _mark(wrapper, POLICY_ADMIN)


def active_required(func):
    """Require an authenticated AND active account.

    ``graphql_jwt``'s ``login_required`` only checks ``is_authenticated``. In
    practice graphql_jwt also refuses an inactive user's token at decode time, so
    this is defence in depth rather than the only barrier — it still holds if
    that behaviour or the auth backend ever changes.
    """

    @wraps(func)
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated:
            raise AuthenticationRequired("Authentication required.")
        if not user.is_active:
            raise PermissionDenied("This account is deactivated.")
        return func(self, info, *args, **kwargs)

    return _mark(wrapper, POLICY_ACTIVE)


def login_required(func):
    """Authenticated, any role or state."""

    @wraps(func)
    def wrapper(self, info, *args, **kwargs):
        if not info.context.user.is_authenticated:
            raise AuthenticationRequired("Authentication required.")
        return func(self, info, *args, **kwargs)

    return _mark(wrapper, POLICY_AUTHENTICATED)


def station_owner_required(func):
    """Require an approved, active station owner.

    Approval is checked here rather than at each call site, so every station and
    booking-management path picks up the lifecycle for free. A pending owner is a
    real, signed-in user — they simply cannot manage stations yet, and the
    message says which of the three states they are in.
    """

    @wraps(func)
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated:
            raise AuthenticationRequired("Authentication required.")
        if not user.is_active:
            raise PermissionDenied("This account is deactivated.")
        if user.role != 'station_owner':
            raise PermissionDenied("Only approved station owners can perform this action.")

        from .models import User as UserModel

        if user.owner_status == UserModel.OWNER_PENDING:
            raise PermissionDenied(
                "Your station owner account is awaiting approval."
            )
        if user.owner_status == UserModel.OWNER_REJECTED:
            raise PermissionDenied(
                "Your station owner application was not approved."
            )
        if user.owner_status != UserModel.OWNER_APPROVED:
            raise PermissionDenied("Only approved station owners can perform this action.")
        return func(self, info, *args, **kwargs)

    return _mark(wrapper, POLICY_OWNER)
