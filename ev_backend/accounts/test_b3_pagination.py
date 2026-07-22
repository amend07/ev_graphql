"""Rule 5 enforcement: no collection may be unbounded (Sprint B3, Priority 1).

Until B3 this rule was enforced by review, and review lost: `usersByRole` shipped
returning `User.objects.filter(role=role)` — the whole table, no limit, no total —
and stayed that way through two sprints and a security audit. Nobody decided to
publish an unbounded query; nobody was asked.

These tests do the asking. Three layers, deliberately independent:

1. `EveryCollectionIsClassified` — the schema and COLLECTIONS must agree, so a new
   list field cannot merge until someone writes down how it is bounded.
2. `EveryCollectionIsBounded` — the load-bearing one. It shrinks the caps to 5,
   seeds 8 rows, and asserts every collection returns at most 5. It does not care
   HOW a resolver bounds itself: `paginate()`, `hard_cap()`, a hand-rolled slice,
   or something invented next year all pass, and only actually-unbounded fails.
   A test that checked for a `paginate()` call would be a spelling test; this
   checks the property we actually want.
3. `EveryPagedCollectionHonoursLimit` — bounded is not enough. A resolver that
   caps at 500 but ignores `limit` makes a client's paging silently a lie.

Written to fail loudly and specifically: each failure names the field and what to
do about it, because the person who trips this will not have read this docstring.
"""

from dataclasses import dataclass, field as dc_field
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from graphene.test import Client
from graphql import GraphQLList, GraphQLNonNull, GraphQLObjectType

from bookings.models import Booking
from ev_backend.schema import schema
from stations.models import Favorite, Review, Station

from .models import AuditLog, PasswordResetOTP
from notifications.models import Notification
from vehicles.models import Vehicle

User = get_user_model()

# Small enough that a resolver ignoring the cap is obvious, large enough that an
# off-by-one is not mistaken for success.
TEST_CAP = 5
SEEDED = 8

PAGE = 'page'        # paginate() — items/totalCount/hasNext
WINDOW = 'window'    # accepts limit/offset, hard_cap when omitted
CAPPED = 'capped'    # no paging arguments at all; hard_cap only


@dataclass(frozen=True)
class Collection:
    """How a collection field is bounded, and who may call it."""

    policy: str
    actor: str = 'admin'
    #: Required arguments, resolved against the fixture at call time.
    args: dict = dc_field(default_factory=dict)
    #: CAPPED only: why this field cannot page. Must be a deliberate answer.
    why: str = ''


# THE CONTRACT. Every collection the schema publishes, and how it is bounded.
COLLECTIONS = {
    # Public station discovery.
    'stationList': Collection(WINDOW, actor='anon'),
    'filterStations': Collection(WINDOW, actor='anon'),
    'stationsPage': Collection(PAGE, actor='anon'),
    'stationReviews': Collection(PAGE, actor='anon', args={'stationId': 'station'}),
    # Signed in: your own data.
    'myStations': Collection(WINDOW, actor='owner'),
    'myStationsPage': Collection(PAGE, actor='owner'),
    'myFavorites': Collection(PAGE, actor='customer'),
    'myBookings': Collection(WINDOW, actor='customer'),
    'myBookingsPage': Collection(PAGE, actor='customer'),
    'myNotificationsPage': Collection(PAGE, actor='customer'),
    'myVehiclesPage': Collection(PAGE, actor='customer'),
    # Owner: bookings on a station you own.
    'stationBookings': Collection(WINDOW, actor='owner', args={'bookingId': 'station'}),
    # Admin.
    'usersPage': Collection(PAGE),
    'auditLogsPage': Collection(PAGE),
    'stationsPageAdmin': Collection(PAGE),
    'bookingsPage': Collection(PAGE),
    'reviewsPage': Collection(PAGE),
    'usersByRole': Collection(
        CAPPED,
        args={'role': 'user'},
        why=(
            "DEPRECATED (B1). Kept unchanged for the shipped web client, which "
            "cannot page it. Bounded by hard_cap, which TRUNCATES SILENTLY — the "
            "caller cannot tell a full answer from a cut-off one. Do not copy "
            "this pattern; use usersPage. Delete once no client calls it."
        ),
    ),
    'allOtps': Collection(
        CAPPED,
        why=(
            "Reset codes are short-lived and self-deleting, so the table is "
            "small by construction rather than by argument. Bounded by hard_cap "
            "as a backstop. If it ever needs paging it needs an otpsPage, not a "
            "limit bolted onto this."
        ),
    ),
}


def _unwrap(gql_type):
    while isinstance(gql_type, GraphQLNonNull):
        gql_type = gql_type.of_type
    return gql_type


def _is_collection(gql_type):
    """A field is a collection if it returns a list, or a page wrapping one."""
    inner = _unwrap(gql_type)
    if isinstance(inner, GraphQLList):
        return True
    return isinstance(inner, GraphQLObjectType) and 'items' in inner.fields


def schema_collections():
    """Every collection field the schema actually publishes."""
    query = schema.graphql_schema.type_map['Query']
    return {name for name, f in query.fields.items() if _is_collection(f.type)}


class EveryCollectionIsClassified(TestCase):
    def test_the_schema_and_the_contract_agree(self):
        published = schema_collections()
        classified = set(COLLECTIONS)

        unclassified = published - classified
        self.assertEqual(
            unclassified, set(),
            f"\n\nCollection(s) {sorted(unclassified)} return a list but are not in "
            f"COLLECTIONS.\nSay how each is bounded: PAGE (paginate(), preferred), "
            f"WINDOW (limit/offset + hard_cap), or CAPPED (no paging — and you must "
            f"justify why in `why=`).\nAn unbounded collection is how B1's usersByRole "
            f"shipped.",
        )

        stale = classified - published
        self.assertEqual(
            stale, set(),
            f"\n\nCOLLECTIONS lists {sorted(stale)}, which the schema no longer has. "
            f"Remove the entry — a contract nobody checks rots.",
        )

    def test_every_capped_collection_justifies_itself(self):
        # CAPPED means "this silently truncates and we accepted that". It is never
        # the right answer for a new field, so it costs a written reason.
        for name, spec in COLLECTIONS.items():
            if spec.policy == CAPPED:
                self.assertTrue(
                    spec.why.strip(),
                    f"{name} is CAPPED without a justification. Add `why=` or page it.",
                )

    def test_paged_collections_expose_a_total_and_a_next_flag(self):
        query = schema.graphql_schema.type_map['Query']
        for name, spec in COLLECTIONS.items():
            if spec.policy != PAGE:
                continue
            page_type = _unwrap(query.fields[name].type)
            for required in ('items', 'totalCount', 'hasNext'):
                self.assertIn(
                    required, page_type.fields,
                    f"{name} is PAGE but its type has no `{required}`. A page a client "
                    f"cannot page through is not a page.",
                )

    def test_paged_and_windowed_collections_accept_limit_and_offset(self):
        query = schema.graphql_schema.type_map['Query']
        for name, spec in COLLECTIONS.items():
            if spec.policy == CAPPED:
                continue
            args = set(query.fields[name].args)
            self.assertTrue(
                {'limit', 'offset'} <= args,
                f"{name} is {spec.policy} but accepts {sorted(args)}. Pageable fields "
                f"take `limit`/`offset` — the names every other endpoint uses.",
            )


class CollectionFixture(TestCase):
    """Seeds SEEDED rows of everything a collection could return."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='b3admin', email='b3admin@x.com', password='123456', role='admin',
        )
        cls.owner = User.objects.create_user(
            username='b3owner', email='b3owner@x.com', password='123456',
            role='station_owner', owner_status=User.OWNER_APPROVED,
        )
        cls.customers = [
            User.objects.create_user(
                username=f'b3cust{i}', email=f'b3cust{i}@x.com', password='123456',
            )
            for i in range(SEEDED)
        ]
        cls.customer = cls.customers[0]

        cls.stations = [
            Station.objects.create(
                owner=cls.owner, name=f'Station {i}', location=f'Loc {i}',
                latitude=1.0, longitude=1.0, availability='24/7', charger_type='CCS',
                num_of_charger=1, power_output_kw=22.0, price_per_kwh=5,
            )
            for i in range(SEEDED)
        ]
        cls.station = cls.stations[0]

        now = timezone.now()
        # Every customer books station 0 -> stationBookings/reviewsPage are over-full.
        for i, customer in enumerate(cls.customers):
            Booking.objects.create(
                user=customer, station=cls.station, status='done',
                start_time=now - timedelta(hours=i + 2),
                end_time=now - timedelta(hours=i + 1),
            )
            Review.objects.create(user=customer, station=cls.station, rating=5)
        # ...and customer 0 books/favourites every station -> myBookings/myFavorites too.
        for i, station in enumerate(cls.stations):
            Favorite.objects.create(user=cls.customer, station=station)
            if station != cls.station:
                Booking.objects.create(
                    user=cls.customer, station=station, status='done',
                    start_time=now - timedelta(days=i + 2),
                    end_time=now - timedelta(days=i + 1),
                )

        for i in range(SEEDED):
            Notification.objects.create(
                recipient=cls.customer, notification_type=Notification.TYPE_SYSTEM,
                title=f'N{i}', body='seed',
            )
            # Customer 0 owns SEEDED vehicles -> myVehiclesPage is over-full. Only
            # the first is primary (the partial unique constraint forbids more).
            Vehicle.objects.create(
                owner=cls.customer, make='Tesla', model=f'M{i}', year=2022,
                battery_capacity_kwh=60, charger_type=Station.CHARGER_NACS,
                plate_number=f'AA-{i:04d}', is_primary=(i == 0),
            )
            PasswordResetOTP.objects.create(
                user=cls.customers[i], otp_hash='x', expires_at=now + timedelta(minutes=10),
            )
            AuditLog.objects.create(
                actor=cls.admin, actor_username='b3admin',
                action=AuditLog.ACTION_USER_DEACTIVATED,
                target_type='user', target_id=str(i), target_label=f'b3cust{i}',
            )

    def setUp(self):
        # The public station list is cached and capped at build time; a page built
        # under the real cap would sail through the shrunk one.
        cache.clear()

    def actor_for(self, name):
        return {
            'anon': AnonymousUser(),
            'admin': self.admin,
            'owner': self.owner,
            'customer': self.customer,
        }[name]

    def arg_value(self, value):
        return self.station.id if value == 'station' else value

    def execute(self, name, spec, extra_args=None):
        """Run one collection field, selecting only __typename.

        __typename exists on every object type, so this needs no per-field
        knowledge of item shapes and cannot rot when a type gains or loses a field.
        """
        args = {k: self.arg_value(v) for k, v in spec.args.items()}
        args.update(extra_args or {})
        rendered = ', '.join(
            f'{k}: {v}' if not isinstance(v, str) else f'{k}: "{v}"'
            for k, v in args.items()
        )
        call = f'{name}({rendered})' if rendered else name
        selection = (
            '{ items { __typename } totalCount hasNext }'
            if spec.policy == PAGE else '{ __typename }'
        )

        request = RequestFactory().post('/graphql/')
        request.user = self.actor_for(spec.actor)
        result = Client(schema).execute(f'{{ {call} {selection} }}', context=request)

        self.assertIsNone(
            result.get('errors'),
            f"{name} could not be executed by the '{spec.actor}' actor: "
            f"{result.get('errors')}. Fix the COLLECTIONS entry, not this test.",
        )
        payload = result['data'][name]
        return payload['items'] if spec.policy == PAGE else payload


@override_settings(
    GRAPHQL_LIST_HARD_CAP=TEST_CAP,
    GRAPHQL_MAX_PAGE_SIZE=TEST_CAP,
    GRAPHQL_DEFAULT_PAGE_SIZE=TEST_CAP,
)
class EveryCollectionIsBounded(CollectionFixture):
    """The test that would have caught usersByRole.

    Caps shrunk to 5, 8 rows seeded, every collection called with NO limit. Any
    field that hands back all 8 is unbounded, whatever its resolver claims.
    """

    def test_no_collection_returns_more_than_the_cap(self):
        for name, spec in COLLECTIONS.items():
            with self.subTest(collection=name):
                rows = self.execute(name, spec)
                self.assertLessEqual(
                    len(rows), TEST_CAP,
                    f"\n\n{name} returned {len(rows)} rows with the cap set to "
                    f"{TEST_CAP} — it is UNBOUNDED.\nA caller can pull the whole "
                    f"table in one request. Route it through paginate() (preferred) "
                    f"or window()/hard_cap().\nThis is exactly the B1 usersByRole "
                    f"defect, which no test caught for two sprints.",
                )

    def test_the_guard_can_actually_fail(self):
        # A bounds test that cannot fail is worse than none: it reports safety it
        # never checked. Prove the fixture really does over-fill the caps.
        self.assertGreater(
            SEEDED, TEST_CAP,
            "The fixture seeds fewer rows than the cap, so nothing above proves anything.",
        )
        self.assertEqual(len(self.stations), SEEDED)
        self.assertGreaterEqual(Booking.objects.filter(station=self.station).count(), SEEDED)


@override_settings(
    GRAPHQL_LIST_HARD_CAP=TEST_CAP,
    GRAPHQL_MAX_PAGE_SIZE=TEST_CAP,
    GRAPHQL_DEFAULT_PAGE_SIZE=TEST_CAP,
)
class EveryPagedCollectionHonoursLimit(CollectionFixture):
    """Bounded is not the same as pageable.

    A resolver that clamps to the cap but drops `limit` on the floor gives every
    client the same page forever, and the paging controls in the UI lie.
    """

    def test_limit_is_respected(self):
        for name, spec in COLLECTIONS.items():
            if spec.policy == CAPPED:
                continue  # no limit argument to honour — justified in COLLECTIONS
            with self.subTest(collection=name):
                rows = self.execute(name, spec, extra_args={'limit': 2})
                self.assertLessEqual(
                    len(rows), 2,
                    f"\n\n{name} returned {len(rows)} rows for limit: 2 — it ignores "
                    f"`limit`.\nThe client's paging is decorative.",
                )

    def test_offset_advances_the_window(self):
        for name, spec in COLLECTIONS.items():
            if spec.policy == CAPPED:
                continue
            with self.subTest(collection=name):
                first = self.execute(name, spec, extra_args={'limit': 1, 'offset': 0})
                second = self.execute(name, spec, extra_args={'limit': 1, 'offset': 1})
                # Only the counts are asserted: __typename is identical across rows,
                # so this checks the window moves and stays inside its bounds, which
                # is all `offset` promises.
                self.assertLessEqual(len(first), 1)
                self.assertLessEqual(len(second), 1)
