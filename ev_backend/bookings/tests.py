"""Business-rule tests for bookings (Sprint 4)."""

from datetime import timedelta

from django.test import TestCase, RequestFactory, override_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from graphene.test import Client

from ev_backend.schema import schema
from stations.models import Station
from bookings.models import Booking

User = get_user_model()

CREATE = """
mutation($s:ID!,$a:DateTime!,$b:DateTime!){
  createBooking(stationId:$s, startTime:$a, endTime:$b){ booking { id status } }
}
"""
CANCEL = """
mutation($id:ID!){ cancelBooking(bookingId:$id){ booking { status } } }
"""
UPDATE = """
mutation($id:ID!,$s:String!){ updateBookingStatus(bookingId:$id, status:$s){ booking { status } } }
"""


def make_user(username, role="user", is_active=True):
    return User.objects.create_user(
        username=username, email=f"{username}@x.com",
        password="123456", role=role, is_active=is_active,
    )


def make_station(owner, **kw):
    defaults = dict(
        name="Station", location="Loc", latitude=1.0, longitude=1.0,
        availability="24/7", charger_type="CCS", num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )
    defaults.update(kw)
    return Station.objects.create(owner=owner, **defaults)


class BookingTestBase(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.owner = make_user("owner", role="station_owner")
        self.driver = make_user("driver")
        self.station = make_station(self.owner)

    def ctx(self, user):
        r = RequestFactory().post("/graphql/")
        r.user = user
        return r

    def future(self, **kw):
        return (timezone.now() + timedelta(**kw)).isoformat()

    def book(self, user, start_kw, end_kw, station=None):
        return self.client.execute(
            CREATE,
            variables={"s": str((station or self.station).id),
                       "a": self.future(**start_kw), "b": self.future(**end_kw)},
            context=self.ctx(user),
        )


class CreateBookingRules(BookingTestBase):
    def test_valid_booking(self):
        res = self.book(self.driver, {"hours": 1}, {"hours": 2})
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["createBooking"]["booking"]["status"], "pending")

    def test_start_must_be_in_future(self):
        res = self.client.execute(
            CREATE,
            variables={"s": str(self.station.id),
                       "a": (timezone.now() - timedelta(hours=1)).isoformat(),
                       "b": self.future(hours=1)},
            context=self.ctx(self.driver),
        )
        self.assertIsNotNone(res.get("errors"))

    def test_end_after_start(self):
        res = self.book(self.driver, {"hours": 3}, {"hours": 2})
        self.assertIsNotNone(res.get("errors"))

    def test_min_duration(self):
        res = self.book(self.driver, {"hours": 1}, {"hours": 1, "minutes": 5})
        self.assertIsNotNone(res.get("errors"))

    def test_max_duration(self):
        res = self.book(self.driver, {"hours": 1}, {"hours": 10})
        self.assertIsNotNone(res.get("errors"))

    def test_station_must_exist(self):
        res = self.client.execute(
            CREATE, variables={"s": "999999", "a": self.future(hours=1), "b": self.future(hours=2)},
            context=self.ctx(self.driver),
        )
        self.assertIsNotNone(res.get("errors"))

    def test_inactive_station_rejected(self):
        self.station.is_active = False
        self.station.save()
        res = self.book(self.driver, {"hours": 1}, {"hours": 2})
        self.assertIsNotNone(res.get("errors"))

    def test_owner_cannot_book_own_station(self):
        res = self.book(self.owner, {"hours": 1}, {"hours": 2})
        self.assertIsNotNone(res.get("errors"))

    def test_overlap_blocked_single_charger(self):
        Booking.objects.create(
            user=self.driver, station=self.station, status="approved",
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=3),
        )
        other = make_user("driver2")
        res = self.book(other, {"hours": 2}, {"hours": 4})  # overlaps
        self.assertIsNotNone(res.get("errors"))

    def test_multi_charger_allows_concurrent(self):
        self.station.num_of_charger = 2
        self.station.save()
        Booking.objects.create(
            user=self.driver, station=self.station, status="approved",
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=3),
        )
        other = make_user("driver2")
        res = self.book(other, {"hours": 2}, {"hours": 4})
        self.assertIsNone(res.get("errors"))

    def test_deactivated_user_cannot_book(self):
        dead = make_user("dead", is_active=False)
        res = self.book(dead, {"hours": 1}, {"hours": 2})
        self.assertIsNotNone(res.get("errors"))


class BookingStateTransitions(BookingTestBase):
    def _pending(self):
        return Booking.objects.create(
            user=self.driver, station=self.station, status="pending",
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=2),
        )

    def test_owner_approves_then_completes(self):
        b = self._pending()
        r1 = self.client.execute(UPDATE, variables={"id": str(b.id), "s": "approved"},
                                 context=self.ctx(self.owner))
        self.assertEqual(r1["data"]["updateBookingStatus"]["booking"]["status"], "approved")
        r2 = self.client.execute(UPDATE, variables={"id": str(b.id), "s": "done"},
                                 context=self.ctx(self.owner))
        self.assertEqual(r2["data"]["updateBookingStatus"]["booking"]["status"], "done")

    def test_non_owner_cannot_update_status(self):
        b = self._pending()
        other = make_user("owner2", role="station_owner")
        res = self.client.execute(UPDATE, variables={"id": str(b.id), "s": "approved"},
                                  context=self.ctx(other))
        self.assertIsNotNone(res.get("errors"))

    def test_invalid_transition_rejected(self):
        b = self._pending()
        b.status = "done"
        b.save()
        res = self.client.execute(UPDATE, variables={"id": str(b.id), "s": "approved"},
                                  context=self.ctx(self.owner))
        self.assertIsNotNone(res.get("errors"))

    def test_user_cancels_own_booking(self):
        b = self._pending()
        res = self.client.execute(CANCEL, variables={"id": str(b.id)}, context=self.ctx(self.driver))
        self.assertEqual(res["data"]["cancelBooking"]["booking"]["status"], "cancelled")

    def test_cannot_cancel_others_booking(self):
        b = self._pending()
        other = make_user("driver2")
        res = self.client.execute(CANCEL, variables={"id": str(b.id)}, context=self.ctx(other))
        self.assertIsNotNone(res.get("errors"))

    def test_cannot_cancel_done_booking(self):
        b = self._pending()
        b.status = "done"
        b.save()
        res = self.client.execute(CANCEL, variables={"id": str(b.id)}, context=self.ctx(self.driver))
        self.assertIsNotNone(res.get("errors"))


class BookingQueries(BookingTestBase):
    def test_my_bookings_scoped_to_user(self):
        Booking.objects.create(user=self.driver, station=self.station, status="pending",
                               start_time=timezone.now() + timedelta(hours=1),
                               end_time=timezone.now() + timedelta(hours=2))
        other = make_user("driver2")
        Booking.objects.create(user=other, station=self.station, status="pending",
                               start_time=timezone.now() + timedelta(hours=3),
                               end_time=timezone.now() + timedelta(hours=4))
        q = "{ myBookings { id } }"
        res = self.client.execute(q, context=self.ctx(self.driver))
        self.assertEqual(len(res["data"]["myBookings"]), 1)

    def test_station_bookings_owner_only(self):
        q = "query($id:ID!){ stationBookings(bookingId:$id){ id } }"
        stranger = make_user("owner2", role="station_owner")
        res = self.client.execute(q, variables={"id": str(self.station.id)}, context=self.ctx(stranger))
        self.assertIsNotNone(res.get("errors"))

    def test_my_bookings_page_totalcount_and_limit(self):
        for i in range(3):
            Booking.objects.create(
                user=self.driver, station=self.station, status="pending",
                start_time=timezone.now() + timedelta(hours=i + 1),
                end_time=timezone.now() + timedelta(hours=i + 2),
            )
        q = "query($l:Int){ myBookingsPage(limit:$l){ items { id } totalCount hasNext } }"
        res = self.client.execute(q, variables={"l": 2}, context=self.ctx(self.driver))
        page = res["data"]["myBookingsPage"]
        self.assertEqual(page["totalCount"], 3)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["hasNext"])
