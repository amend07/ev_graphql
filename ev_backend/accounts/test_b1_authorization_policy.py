"""Authorization policy coverage (Sprint B1, Phase 1).

The audit's root cause was not a wrong decision — it was an absent one. Nobody
chose to publish customers' bookings anonymously; `fields = "__all__"` published
them and no test asked who was allowed to read them.

This module makes that impossible to repeat. Every root field is listed below
with the policy it must enforce, and the test fails if the schema and this table
disagree — so a new query or mutation cannot merge until someone writes down who
may call it. Adding a field without classifying it is a failing build, not a
silent hole.
"""

from django.test import TestCase

from ev_backend.schema import schema

from .permission import (
    POLICY_ACTIVE,
    POLICY_ADMIN,
    POLICY_ATTR,
    POLICY_AUTHENTICATED,
    POLICY_OWNER,
    POLICY_PUBLIC,
)

# The authorization contract of the whole API, in one place.
QUERY_POLICY = {
    # Public: station discovery. Deliberately readable by anyone.
    'stationList': POLICY_PUBLIC,
    'filterStations': POLICY_PUBLIC,
    'stationsPage': POLICY_PUBLIC,
    'stationById': POLICY_PUBLIC,
    'stationReviews': POLICY_PUBLIC,
    # Signed in: your own data.
    'me': POLICY_AUTHENTICATED,
    'myStations': POLICY_AUTHENTICATED,
    'myFavorites': POLICY_AUTHENTICATED,
    'myBookings': POLICY_AUTHENTICATED,
    'myBookingsPage': POLICY_AUTHENTICATED,
    # Notifications: your own, for any signed-in user (customer/owner/admin).
    'myNotificationsPage': POLICY_AUTHENTICATED,
    'myUnreadNotificationCount': POLICY_AUTHENTICATED,
    # Owner: another party's data, scoped to a station you own.
    'stationBookings': POLICY_OWNER,
    # Admin.
    'usersByRole': POLICY_ADMIN,
    'usersPage': POLICY_ADMIN,
    'allOtps': POLICY_ADMIN,
    'auditLogsPage': POLICY_ADMIN,
    'userDeletionPreview': POLICY_ADMIN,
    # Admin — platform APIs (B2.1). Admin is the only defensible policy for each:
    # `stationsPageAdmin` deliberately sees inactive stations, `bookingsPage`
    # sees every customer's movements across the platform (the exact data the B1
    # vulnerability leaked), `reviewsPage` sees moderated content, and
    # `dashboardSummary` aggregates all of it.
    'stationsPageAdmin': POLICY_ADMIN,
    'bookingsPage': POLICY_ADMIN,
    'bookingById': POLICY_ADMIN,
    'reviewsPage': POLICY_ADMIN,
    'dashboardSummary': POLICY_ADMIN,
    # Public by necessity (W7): the sign-in screen asks this BEFORE anyone has a
    # session, to decide which buttons it can honestly offer. It discloses which
    # methods this deployment supports — configuration, not user data — which is
    # already inferable by pressing the buttons.
    'authCapabilities': POLICY_PUBLIC,
}

MUTATION_POLICY = {
    # Unauthenticated by necessity: you cannot be signed in to sign in, and
    # these are rate-limited and anti-enumeration hardened instead.
    'createUser': POLICY_PUBLIC,
    'tokenAuth': POLICY_PUBLIC,
    'verifyToken': POLICY_PUBLIC,
    'refreshToken': POLICY_PUBLIC,
    'sendPinResetOtp': POLICY_PUBLIC,
    'resetPinWithOtp': POLICY_PUBLIC,
    'sendPasswordResetOtp': POLICY_PUBLIC,
    'resetPasswordWithOtp': POLICY_PUBLIC,
    # Identity (W7). Public for the same reason as tokenAuth — these ARE the
    # sign-in path, and the person using them has no session yet by definition.
    # The protection is not a policy decorator: it is possession of the handset,
    # plus rate limiting per IP and per normalised number, plus replies that are
    # identical whether or not the number is known.
    'sendPhoneOtp': POLICY_PUBLIC,
    'signInWithPhone': POLICY_PUBLIC,
    # Signed in.
    'changePin': POLICY_AUTHENTICATED,
    'changePassword': POLICY_AUTHENTICATED,
    # Identity management, on your own account only (W7).
    #
    # POLICY_AUTHENTICATED, not POLICY_ACTIVE, and the distinction is deliberate:
    # a deactivated user must still be able to secure their account. Revoking a
    # stolen session is the one thing that must not require being in good
    # standing — and `linkPhone` is what a legacy user does to comply with the
    # §9.1 prompt, which they may be doing precisely because access is limited.
    'linkPhone': POLICY_AUTHENTICATED,
    'unlinkProvider': POLICY_AUTHENTICATED,
    'logoutEverywhere': POLICY_AUTHENTICATED,
    # Phone + PIN and social sign-in (W8). The sign-in / signup entry points are
    # public for the same reason tokenAuth is — the caller has no session yet —
    # and are hardened by rate limiting, single-use emailed OTPs, generic
    # timing-equalised replies (phone+PIN), and provider token verification with
    # an audience allow-list (Google/Apple). setMyPhone is the one signed-in
    # member: it writes to your own account.
    'sendSignupOtp': POLICY_PUBLIC,
    'registerWithPhone': POLICY_PUBLIC,
    'signInWithPhonePin': POLICY_PUBLIC,
    'signInWithGoogle': POLICY_PUBLIC,
    'signInWithApple': POLICY_PUBLIC,
    'setMyPhone': POLICY_AUTHENTICATED,
    # Notifications: mark your own read; any signed-in user.
    'markNotificationRead': POLICY_AUTHENTICATED,
    'markAllNotificationsRead': POLICY_AUTHENTICATED,
    # Active account required for anything that writes domain data.
    'createBooking': POLICY_ACTIVE,
    'cancelBooking': POLICY_ACTIVE,
    'createReview': POLICY_ACTIVE,
    'updateReview': POLICY_ACTIVE,
    'deleteReview': POLICY_ACTIVE,
    'toggleFavoriteStation': POLICY_ACTIVE,
    # Approved station owner.
    'createStation': POLICY_OWNER,
    'updateStation': POLICY_OWNER,
    'deleteStation': POLICY_OWNER,
    'updateBookingStatus': POLICY_OWNER,
    # Admin.
    'approveStationOwner': POLICY_ADMIN,
    'rejectStationOwner': POLICY_ADMIN,
    'toggleUserActive': POLICY_ADMIN,
    'deleteUser': POLICY_ADMIN,
    # Admin — station and review administration (B2.1).
    #
    # `adminDeleteReview` is NOT `deleteReview`. The latter is listed above as
    # POLICY_ACTIVE and is author-only: it is a customer deleting their own
    # words. Giving the admin action the same name would have collapsed two
    # different policies onto one field, which is precisely the ambiguity this
    # table exists to prevent.
    'activateStation': POLICY_ADMIN,
    'deactivateStation': POLICY_ADMIN,
    'hideReview': POLICY_ADMIN,
    'restoreReview': POLICY_ADMIN,
    'adminDeleteReview': POLICY_ADMIN,
}

# The auth mutations above are `graphql_jwt`'s own or are intentionally
# unauthenticated entry points; they carry no marker of ours to read.
UNMARKED_BY_DESIGN = {
    'createUser', 'tokenAuth', 'verifyToken', 'refreshToken',
    'sendPinResetOtp', 'resetPinWithOtp',
    'sendPasswordResetOtp', 'resetPasswordWithOtp',
}


def _root_fields(name):
    return set(schema.graphql_schema.type_map[name].fields)


class SchemaIsFullyClassified(TestCase):
    def test_every_query_is_listed_with_a_policy(self):
        self.assertEqual(
            _root_fields('Query'),
            set(QUERY_POLICY),
            "A query is missing from QUERY_POLICY: decide who may call it.",
        )

    def test_every_mutation_is_listed_with_a_policy(self):
        self.assertEqual(
            _root_fields('Mutation'),
            set(MUTATION_POLICY),
            "A mutation is missing from MUTATION_POLICY: decide who may call it.",
        )


class ResolversEnforceTheirDeclaredPolicy(TestCase):
    """The table above is a claim; this checks the code backs it."""

    def _assert_marked(self, resolver, expected, field):
        actual = getattr(resolver, POLICY_ATTR, None)
        self.assertIsNotNone(
            actual,
            f"{field} has no authorization decorator — it is protected by nothing.",
        )
        self.assertEqual(actual, expected, f"{field} enforces {actual}, not {expected}")

    def test_query_resolvers_carry_the_declared_marker(self):
        query_type = schema.graphql_schema.type_map['Query']
        for field, expected in QUERY_POLICY.items():
            resolver = query_type.fields[field].resolve
            self._assert_marked(resolver, expected, f"Query.{field}")

    def test_mutation_resolvers_carry_the_declared_marker(self):
        mutation_type = schema.graphql_schema.type_map['Mutation']
        for field, expected in MUTATION_POLICY.items():
            if field in UNMARKED_BY_DESIGN:
                continue
            mutation_class = mutation_type.fields[field].type.graphene_type
            self._assert_marked(mutation_class.mutate, expected, f"Mutation.{field}")

    def test_no_field_reaching_private_data_is_public(self):
        # A public marker on anything but station discovery is the exact mistake
        # B1 fixed: `stationById` was public AND traversed to bookings.
        allowed_public = {
            'stationList', 'filterStations', 'stationsPage', 'stationById',
            'stationReviews',
            # W7. The one non-discovery public query, admitted deliberately and
            # on a narrower ground than the others: it returns four booleans
            # about this deployment's own configuration and reaches no model at
            # all. It cannot traverse to user data because it does not traverse.
            #
            # Adding to this set must stay expensive. The question to answer
            # before the next entry is not "is it convenient" but "what can an
            # anonymous caller reach THROUGH it" — B1's leak was not a field that
            # held private data, it was a public field that could walk to it.
            'authCapabilities',
        }
        actually_public = {f for f, p in QUERY_POLICY.items() if p == POLICY_PUBLIC}
        self.assertEqual(actually_public, allowed_public)
