"""Query-count regressions (Sprint B3, Priority 5).

N+1 is invisible in code review and invisible in tests that only assert content:
every page returns the right rows either way, and the cost only shows up as a
slow admin screen nobody can explain. It is also invisible in a dev database with
ten rows, which is where it always gets written.

So the count is asserted. These tests do not measure time — they measure the
thing that makes time explode: whether the work per page grows with the number of
rows on it.

Each page is queried at two sizes. **That is the whole point.** A fixed
`assertNumQueries(4)` passes an N+1 that happens to be N=4 today. Asserting the
count does not CHANGE with the row count catches it regardless of the constant,
and needs no updating when someone legitimately adds a join.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from graphene.test import Client

from bookings.models import Booking
from ev_backend.schema import schema
from stations.models import Review, Station

User = get_user_model()

SMALL, LARGE = 2, 12


class QueryCountFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='qcadmin', email='qcadmin@x.com', password='123456', role='admin',
        )
        cls.owner = User.objects.create_user(
            username='qcowner', email='qcowner@x.com', password='123456',
            role='station_owner', owner_status=User.OWNER_APPROVED,
        )
        cls.customers = [
            User.objects.create_user(
                username=f'qccust{i}', email=f'qccust{i}@x.com', password='123456',
            )
            for i in range(LARGE)
        ]
        cls.stations = [
            Station.objects.create(
                owner=cls.owner, name=f'QC {i}', location='Loc', latitude=1.0,
                longitude=1.0, availability='24/7', charger_type='CCS',
                num_of_charger=1, power_output_kw=22.0, price_per_kwh=5,
            )
            for i in range(LARGE)
        ]
        now = timezone.now()
        for i, customer in enumerate(cls.customers):
            Review.objects.create(user=customer, station=cls.stations[i], rating=5)
            for station in cls.stations:
                Booking.objects.create(
                    user=customer, station=station, status='done',
                    start_time=now - timedelta(hours=i + 2),
                    end_time=now - timedelta(hours=i + 1),
                )

    def count_queries(self, query, user=None):
        request = RequestFactory().post('/graphql/')
        request.user = user or self.admin
        with CaptureQueriesContext(connection) as captured:
            result = Client(schema).execute(query, context=request)
        self.assertIsNone(result.get('errors'), result.get('errors'))
        return len(captured.captured_queries)

    def assert_flat(self, label, template, user=None):
        """The query count must not grow with the number of rows returned."""
        small = self.count_queries(template % SMALL, user)
        large = self.count_queries(template % LARGE, user)
        self.assertEqual(
            small, large,
            f"\n\n{label} runs {small} queries for {SMALL} rows but {large} for "
            f"{LARGE} — it is N+1.\nThe per-row work is a query. Annotate the "
            f"queryset (see stations.models.review_stats) or select_related the "
            f"join, and make the resolver READ the annotation instead of "
            f"re-querying.\n",
        )


class AdminPagesDoNotScaleQueriesWithRows(QueryCountFixture):
    def test_stations_page_admin_is_flat(self):
        # The regression this test was written for: these resolvers ignored the
        # review_stats() annotation the queryset had already computed and
        # re-queried per row — 22 queries for 10 stations.
        self.assert_flat(
            'stationsPageAdmin',
            '{ stationsPageAdmin(limit: %d) { items { id name averageRating '
            'numOfReviews owner { username } } totalCount } }',
        )

    def test_reviews_page_is_flat(self):
        self.assert_flat(
            'reviewsPage',
            '{ reviewsPage(limit: %d) { items { id rating user { username } '
            'station { id name } } totalCount } }',
        )

    def test_bookings_page_is_flat(self):
        self.assert_flat(
            'bookingsPage',
            '{ bookingsPage(limit: %d) { items { id status user { username email } '
            'station { id name } } totalCount } }',
        )

    def test_users_page_is_flat(self):
        self.assert_flat(
            'usersPage', '{ usersPage(limit: %d) { items { id username role } totalCount } }',
        )


class CustomerAndOwnerPagesDoNotScaleQueriesWithRows(QueryCountFixture):
    def test_public_stations_page_is_flat(self):
        self.assert_flat(
            'stationsPage',
            '{ stationsPage(limit: %d) { items { stationId name averageRate '
            'numOfRate } totalCount } }',
        )

    def test_station_reviews_is_flat(self):
        self.assert_flat(
            'stationReviews',
            '{ stationReviews(stationId: ' + str(1) + ', limit: %d) '
            '{ items { id rating user { username } } totalCount } }',
        )

    def test_my_bookings_page_is_flat(self):
        self.assert_flat(
            'myBookingsPage',
            '{ myBookingsPage(limit: %d) { items { id station { id name } } totalCount } }',
            user=self.customers[0],
        )


class DashboardSummaryCostIsConstant(QueryCountFixture):
    def test_it_does_not_grow_with_the_platform(self):
        """The dashboard's whole job is to describe a large platform.

        If its cost tracked the row count it would get slower exactly as the
        numbers on it got interesting. Every figure is a DB aggregate, so the
        query count is fixed no matter how much data exists — this asserts that
        rather than trusting it.
        """
        query = '''
            { dashboardSummary { customers owners pendingOwners rejectedOwners
                stations activeStations inactiveStations bookingsToday
                bookingsThisWeek bookingsThisMonth reviews averageRating
                newestUsers { id } newestStations { id } } }
        '''
        before = self.count_queries(query)

        now = timezone.now()
        extra = [
            User.objects.create_user(
                username=f'qcbulk{i}', email=f'qcbulk{i}@x.com', password='123456',
            )
            for i in range(10)
        ]
        for customer in extra:
            Booking.objects.create(
                user=customer, station=self.stations[0], status='done',
                start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1),
            )

        after = self.count_queries(query)
        self.assertEqual(
            before, after,
            f"dashboardSummary ran {before} queries, then {after} after adding rows. "
            f"A summary must cost the same whatever it is summarising.",
        )
