"""Booking administration (Sprint B2.1). Read-only, by design.

`bookingsPage` is the single most sensitive read added this sprint: it returns
every customer's identity and movements across the whole platform. That is
exactly the data the B1 vulnerability exposed anonymously through
`stationById { bookings { user { email } } }` — so the authorization tests here
are not box-ticking, they are the same finding approached from the front door.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.utils import timezone
from graphene.test import Client

from ev_backend.errors import NotFound
from ev_backend.schema import schema
from stations.models import Station

from .models import Booking
from .schema import BookingAdminQuery

User = get_user_model()


def make_user(username, role="user", **kw):
    if role == "station_owner":
        kw.setdefault("owner_status", User.OWNER_APPROVED)
    return User.objects.create_user(
        username=username, email=f"{username}@x.com", password="123456",
        role=role, **kw,
    )


def make_station(owner, **kw):
    defaults = dict(
        name="Station", location="Loc", latitude=1.0, longitude=1.0,
        availability="24/7", charger_type="CCS", num_of_charger=5,
        power_output_kw=22.0, price_per_kwh=5,
    )
    defaults.update(kw)
    return Station.objects.create(owner=owner, **defaults)


def run(query, user=None, **variables):
    request = RequestFactory().post("/graphql/")
    request.user = user or AnonymousUser()
    return Client(schema).execute(query, context=request, variables=variables or None)


class BookingsPageAdministration(TestCase):
    def setUp(self):
        self.admin = make_user("badmin", role="admin")
        self.owner_a = make_user("bowner_a", role="station_owner")
        self.owner_b = make_user("bowner_b", role="station_owner")
        self.cust1 = make_user("bcust1")
        self.cust2 = make_user("bcust2")
        self.station_a = make_station(self.owner_a, name="Alpha")
        self.station_b = make_station(self.owner_b, name="Beta")

        now = timezone.now()
        self.soon = Booking.objects.create(
            user=self.cust1, station=self.station_a, status="pending",
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=1, hours=1),
        )
        self.later = Booking.objects.create(
            user=self.cust2, station=self.station_a, status="approved",
            start_time=now + timedelta(days=10),
            end_time=now + timedelta(days=10, hours=1),
        )
        self.past = Booking.objects.create(
            user=self.cust1, station=self.station_b, status="done",
            start_time=now - timedelta(days=5),
            end_time=now - timedelta(days=5) + timedelta(hours=1),
        )

    QUERY = """
        query($status: String, $customerId: ID, $ownerId: ID, $stationId: ID,
              $dateFrom: DateTime, $dateTo: DateTime, $search: String,
              $orderBy: String, $limit: Int, $offset: Int) {
          bookingsPage(status: $status, customerId: $customerId, ownerId: $ownerId,
                       stationId: $stationId, dateFrom: $dateFrom, dateTo: $dateTo,
                       search: $search, orderBy: $orderBy, limit: $limit, offset: $offset) {
            items { id status user { id username email } station { id name } }
            totalCount
            hasNext
          }
        }
    """

    def test_an_admin_sees_every_booking_on_the_platform(self):
        res = run(self.QUERY, user=self.admin)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 3)

    def test_anonymous_is_refused(self):
        res = run(self.QUERY)
        self.assertIsNotNone(res.get("errors"))

    def test_a_customer_is_refused(self):
        # Even though some of these bookings are theirs — `myBookingsPage` is the
        # self-scoped route. This one is not scoped to anybody.
        res = run(self.QUERY, user=self.cust1)
        self.assertIsNotNone(res.get("errors"))

    def test_a_station_owner_is_refused(self):
        # An owner may read bookings AT THEIR OWN STATION via `stationBookings`.
        # Platform-wide is a different question and the answer is no: this would
        # hand them their competitors' customers.
        res = run(self.QUERY, user=self.owner_a)
        self.assertIsNotNone(res.get("errors"))

    def test_an_owner_cannot_widen_their_reach_with_the_owner_filter(self):
        res = run(self.QUERY, user=self.owner_a, ownerId=self.owner_a.id)
        self.assertIsNotNone(res.get("errors"), "the filter is not an authorization check")

    def test_it_filters_by_status(self):
        res = run(self.QUERY, user=self.admin, status="done")
        page = res["data"]["bookingsPage"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual(page["items"][0]["id"], str(self.past.id))

    def test_it_filters_by_customer(self):
        res = run(self.QUERY, user=self.admin, customerId=self.cust1.id)
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 2)

    def test_it_filters_by_station_owner(self):
        res = run(self.QUERY, user=self.admin, ownerId=self.owner_b.id)
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 1)

    def test_it_filters_by_station(self):
        res = run(self.QUERY, user=self.admin, stationId=self.station_a.id)
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 2)

    def test_it_filters_by_date_range_on_the_slot(self):
        # dateFrom/dateTo mean "the booking happens in this window", not "was
        # created in it" — the documented contract.
        res = run(self.QUERY, user=self.admin, dateFrom=timezone.now().isoformat())
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 2, "future only")

        res = run(self.QUERY, user=self.admin, dateTo=timezone.now().isoformat())
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 1, "past only")

        res = run(
            self.QUERY, user=self.admin,
            dateFrom=timezone.now().isoformat(),
            dateTo=(timezone.now() + timedelta(days=2)).isoformat(),
        )
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 1, "bounded window")

    def test_it_searches_customer_and_station(self):
        self.assertEqual(
            run(self.QUERY, user=self.admin, search="bcust2")["data"]["bookingsPage"]["totalCount"], 1,
        )
        self.assertEqual(
            run(self.QUERY, user=self.admin, search="BCUST1@X.COM")["data"]["bookingsPage"]["totalCount"], 2,
        )
        self.assertEqual(
            run(self.QUERY, user=self.admin, search="Beta")["data"]["bookingsPage"]["totalCount"], 1,
        )

    def test_filters_combine(self):
        res = run(self.QUERY, user=self.admin, customerId=self.cust1.id, status="pending")
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 1)

    def test_it_orders_within_the_allow_list(self):
        res = run(self.QUERY, user=self.admin, orderBy="start_time", limit=1)
        self.assertEqual(res["data"]["bookingsPage"]["items"][0]["id"], str(self.later.id))

    def test_an_unknown_order_falls_back_instead_of_erroring(self):
        res = run(self.QUERY, user=self.admin, orderBy="user__password")
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["bookingsPage"]["totalCount"], 3)

    def test_it_pages_with_a_total_and_a_next_flag(self):
        first = run(self.QUERY, user=self.admin, limit=2, offset=0)["data"]["bookingsPage"]
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first["totalCount"], 3)
        self.assertTrue(first["hasNext"])

        second = run(self.QUERY, user=self.admin, limit=2, offset=2)["data"]["bookingsPage"]
        self.assertFalse(second["hasNext"])
        # Tie-broken sort: no row may repeat across pages.
        ids = [r["id"] for r in first["items"]] + [r["id"] for r in second["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_page_size_is_clamped(self):
        res = run(self.QUERY, user=self.admin, limit=10_000)
        self.assertLessEqual(len(res["data"]["bookingsPage"]["items"]), 100)

    def test_the_admin_sees_the_customers_contact_details(self):
        res = run(self.QUERY, user=self.admin, customerId=self.cust2.id)
        self.assertEqual(res["data"]["bookingsPage"]["items"][0]["user"]["email"], "bcust2@x.com")

    def test_a_booking_still_refuses_to_expose_a_privileged_user_field(self):
        # BookingCustomerType is identity + email and nothing else, on every
        # route — including the new admin one.
        res = run(
            "{ bookingsPage { items { user { isSuperuser } } } }", user=self.admin,
        )
        self.assertIsNotNone(res.get("errors"))


class BookingByIdAdministration(TestCase):
    def setUp(self):
        self.admin = make_user("badmin2", role="admin")
        self.owner = make_user("bowner2", role="station_owner")
        self.customer = make_user("bcust3")
        self.stranger = make_user("bstranger")
        self.station = make_station(self.owner)
        self.booking = Booking.objects.create(
            user=self.customer, station=self.station, status="pending",
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=1),
        )

    QUERY = ("query($id: ID!) { bookingById(bookingId: $id) "
             "{ id status user { username email } station { name } } }")

    def test_an_admin_can_read_one_booking(self):
        res = run(self.QUERY, user=self.admin, id=self.booking.id)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["bookingById"]["user"]["email"], "bcust3@x.com")

    def test_anonymous_customer_owner_are_all_refused(self):
        for user in (None, self.customer, self.stranger, self.owner):
            res = run(self.QUERY, user=user, id=self.booking.id)
            self.assertIsNotNone(res.get("errors"))

    def test_a_missing_booking_is_reported_as_not_found(self):
        res = run(self.QUERY, user=self.admin, id=999999)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("not found", res["errors"][0]["message"].lower())

    def test_a_missing_booking_raises_the_typed_refusal(self):
        # `extensions.code` is attached by HardenedGraphQLView, which
        # `graphene.test.Client` does not run — it returns formatted dicts. So
        # the typed refusal (rule 11) is asserted at the resolver instead of
        # through a code path this harness cannot reach.
        request = RequestFactory().post("/graphql/")
        request.user = self.admin
        info = type("Info", (), {"context": request})()
        with self.assertRaises(NotFound):
            BookingAdminQuery.resolve_booking_by_id(None, info, booking_id=999999)


class BookingAdministrationIsReadOnly(TestCase):
    """No admin override, no refund — and the schema says so, not just a comment."""

    def test_there_is_no_admin_booking_mutation(self):
        mutations = set(schema.graphql_schema.type_map["Mutation"].fields)
        for invented in ("adminCancelBooking", "overrideBookingStatus", "refundBooking",
                         "adminUpdateBookingStatus"):
            self.assertNotIn(invented, mutations)

    def test_no_type_in_the_schema_pretends_a_booking_carries_money(self):
        # The Booking model has no price, no payment reference and no refund
        # state. A money field here could only ever be fabricated.
        booking_fields = set(schema.graphql_schema.type_map["BookingType"].fields)
        for invented in ("amount", "price", "total", "paid", "refunded", "currency"):
            self.assertNotIn(invented, booking_fields)
