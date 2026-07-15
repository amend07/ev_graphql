"""Security and authorization tests (Sprint B1, Phases 1 & 7).

Every test here is a regression test for a verified finding from the backend
audit. The traversal cases in particular are the exact queries that answered for
anonymous callers before B1 — they are the reason this sprint exists.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.utils import timezone
from graphene.test import Client

from bookings.models import Booking
from ev_backend.schema import schema
from stations.models import Review, Station

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
        availability="24/7", charger_type="CCS", num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )
    defaults.update(kw)
    return Station.objects.create(owner=owner, **defaults)


def run(query, user=None, **variables):
    request = RequestFactory().post("/graphql/")
    request.user = user or AnonymousUser()
    return Client(schema).execute(query, context=request, variables=variables or None)


class AnonymousTraversalRegression(TestCase):
    """The vulnerability B1 exists to close.

    Before B1: `stationById { bookings { user { email } } }` returned every
    customer's identity and movements to anyone with the URL, bypassing the
    owner-only gate on `stationBookings` entirely.
    """

    def setUp(self):
        self.owner = make_user("owner1", role="station_owner", is_superuser=True)
        self.station = make_station(self.owner)
        self.customer = make_user("cust1")
        Booking.objects.create(
            user=self.customer, station=self.station, status="done",
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=2),
        )
        Review.objects.create(
            user=self.customer, station=self.station, rating=5, comment="Good",
        )

    def test_station_no_longer_exposes_its_bookings(self):
        res = run("query($id: ID!) { stationById(stationId: $id) { bookings { id } } }",
                  id=self.station.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("bookings", res["errors"][0]["message"])

    def test_station_no_longer_exposes_its_reviews_relation(self):
        res = run("query($id: ID!) { stationById(stationId: $id) { reviews { id } } }",
                  id=self.station.id)
        self.assertIsNotNone(res.get("errors"))

    def test_station_no_longer_exposes_who_favourited_it(self):
        res = run("query($id: ID!) { stationById(stationId: $id) { favoritedBy { id } } }",
                  id=self.station.id)
        self.assertIsNotNone(res.get("errors"))

    def test_anonymous_cannot_read_owner_email_through_a_station(self):
        res = run("query($id: ID!) { stationById(stationId: $id) { owner { email } } }",
                  id=self.station.id)
        self.assertIsNotNone(res.get("errors"))

    def test_anonymous_cannot_read_owner_privilege_flags(self):
        for field in ("isSuperuser", "isStaff", "lastLogin", "isActive", "role"):
            res = run(
                "query($id: ID!) { stationById(stationId: $id) { owner { %s } } }" % field,
                id=self.station.id,
            )
            self.assertIsNotNone(res.get("errors"), f"owner.{field} is still reachable")

    def test_anonymous_cannot_read_reviewer_email(self):
        res = run("query($id: ID!) { stationReviews(stationId: $id) { items { user { email } } } }",
                  id=self.station.id)
        self.assertIsNotNone(res.get("errors"))

    def test_anonymous_cannot_reach_otp_metadata_through_a_user(self):
        res = run(
            "query($id: ID!) { stationById(stationId: $id) { owner { passwordresetotpSet { attempts } } } }",
            id=self.station.id,
        )
        self.assertIsNotNone(res.get("errors"))

    def test_public_station_data_is_still_public(self):
        # The fix must not make the product worse: browsing stations is public.
        res = run(
            """
            query($id: ID!) {
              stationById(stationId: $id) {
                id name location latitude longitude pricePerKwh
                chargerType numOfCharger averageRating numOfReviews
                owner { id username }
              }
            }
            """,
            id=self.station.id,
        )
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["stationById"]["owner"]["username"], "owner1")

    def test_public_reviews_still_show_their_author_display_name(self):
        res = run(
            "query($id: ID!) { stationReviews(stationId: $id) { items { rating user { id username } } } }",
            id=self.station.id,
        )
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["stationReviews"]["items"][0]["user"]["username"], "cust1")


def type_fields(name):
    """Field names published by a GraphQL type, straight from the built schema."""
    return set(schema.graphql_schema.type_map[name].fields)


class UserTypeExposure(TestCase):
    """`UserType` is only reachable from self/admin roots — this pins its fields."""

    def test_user_type_publishes_only_the_reviewed_field_list(self):
        self.assertEqual(
            type_fields("UserType"),
            {
                "id", "username", "email", "role", "isActive", "ownerStatus",
                "isStaff", "isSuperuser", "dateJoined", "lastLogin",
                "isStationOwner",
            },
        )

    def test_user_type_never_exposes_the_credential_or_reverse_relations(self):
        fields = type_fields("UserType")
        for leaked in ("password", "passwordresetotpSet", "stations", "bookings",
                       "reviewSet", "favorites", "groups", "userPermissions",
                       "firstName", "lastName"):
            self.assertNotIn(leaked, fields)

    def test_public_user_type_is_identity_only(self):
        self.assertEqual(type_fields("PublicUserType"), {"id", "username"})

    def test_booking_customer_type_carries_contact_but_nothing_privileged(self):
        self.assertEqual(
            type_fields("BookingCustomerType"),
            {"id", "username", "email"},
        )

    def test_station_type_publishes_no_reverse_relations(self):
        fields = type_fields("StationType")
        for leaked in ("bookings", "reviews", "favoritedBy"):
            self.assertNotIn(leaked, fields)


class BookingAuthorization(TestCase):
    def setUp(self):
        self.owner = make_user("owner2", role="station_owner")
        self.other_owner = make_user("owner3", role="station_owner")
        self.station = make_station(self.owner)
        self.customer = make_user("cust2")
        self.stranger = make_user("cust3")
        self.booking = Booking.objects.create(
            user=self.customer, station=self.station, status="pending",
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=1),
        )

    QUERY = "query($id: ID!) { stationBookings(bookingId: $id) { id user { email } } }"

    def test_anonymous_is_refused(self):
        res = run(self.QUERY, id=self.station.id)
        self.assertIsNotNone(res.get("errors"))

    def test_customer_is_refused(self):
        res = run(self.QUERY, user=self.customer, id=self.station.id)
        self.assertIsNotNone(res.get("errors"))

    def test_another_owner_is_refused(self):
        res = run(self.QUERY, user=self.other_owner, id=self.station.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("do not own", res["errors"][0]["message"])

    def test_the_station_owner_sees_the_customer(self):
        res = run(self.QUERY, user=self.owner, id=self.station.id)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["stationBookings"][0]["user"]["email"], "cust2@x.com")

    def test_a_customer_only_ever_sees_their_own_bookings(self):
        res = run("{ myBookingsPage { items { id } totalCount } }", user=self.stranger)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["myBookingsPage"]["totalCount"], 0)
