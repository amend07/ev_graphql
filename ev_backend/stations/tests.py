"""Validation, authorization, review, and favorites tests (Sprint 4)."""

from datetime import timedelta

from django.test import TestCase, RequestFactory, override_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from graphene.test import Client

from ev_backend.schema import schema
from stations.models import Station, Review, Favorite
from bookings.models import Booking

User = get_user_model()


def make_user(username, role="user", is_active=True, owner_status=None):
    """Test user.

    Station owners default to APPROVED: these suites test station and booking
    rules, not the approval lifecycle (see accounts.tests for that), and an
    unapproved owner is refused before any of those rules are reached. Pass
    owner_status explicitly to exercise a pending/rejected owner.
    """
    if role == "station_owner" and owner_status is None:
        owner_status = User.OWNER_APPROVED
    return User.objects.create_user(
        username=username, email=f"{username}@x.com",
        password="123456", role=role, is_active=is_active,
        owner_status=owner_status,
    )


def make_station(owner, **kw):
    defaults = dict(
        name="Station", location="Loc", latitude=1.0, longitude=1.0,
        availability="24/7", charger_type="CCS", num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )
    defaults.update(kw)
    return Station.objects.create(owner=owner, **defaults)


def done_booking(user, station):
    return Booking.objects.create(
        user=user, station=station, status="done",
        start_time=timezone.now() - timedelta(hours=3),
        end_time=timezone.now() - timedelta(hours=2),
    )


class StationTestBase(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.owner = make_user("owner", role="station_owner")

    def ctx(self, user):
        r = RequestFactory().post("/graphql/")
        r.user = user
        return r


VALID_INPUT = {
    "name": "Alpha", "location": "Downtown", "latitude": 9.0, "longitude": 38.0,
    "availability": "24/7", "chargerType": "CCS", "numOfCharger": 2,
    "powerOutputKw": 50.0, "pricePerKwh": 7.5,
}
CREATE_STATION = "mutation($in:CreateStationInput!){ createStation(input:$in){ station { id name } } }"


class StationValidation(StationTestBase):
    def _create(self, user, **overrides):
        payload = dict(VALID_INPUT)
        payload.update(overrides)
        return self.client.execute(CREATE_STATION, variables={"in": payload}, context=self.ctx(user))

    def test_owner_creates_valid_station(self):
        res = self._create(self.owner)
        self.assertIsNone(res.get("errors"))
        self.assertTrue(Station.objects.filter(name="Alpha", owner=self.owner).exists())

    def test_non_owner_cannot_create(self):
        res = self._create(make_user("regular"))
        self.assertIsNotNone(res.get("errors"))

    def test_invalid_latitude(self):
        self.assertIsNotNone(self._create(self.owner, latitude=200.0).get("errors"))

    def test_invalid_longitude(self):
        self.assertIsNotNone(self._create(self.owner, longitude=-500.0).get("errors"))

    def test_negative_price(self):
        self.assertIsNotNone(self._create(self.owner, pricePerKwh=-1.0).get("errors"))

    def test_non_positive_power(self):
        self.assertIsNotNone(self._create(self.owner, powerOutputKw=0.0).get("errors"))

    def test_blank_name(self):
        self.assertIsNotNone(self._create(self.owner, name="   ").get("errors"))

    def test_duplicate_station_rejected(self):
        self._create(self.owner)
        self.assertIsNotNone(self._create(self.owner).get("errors"))

    def test_update_requires_ownership(self):
        station = make_station(self.owner, name="Beta", location="X")
        stranger = make_user("owner2", role="station_owner")
        q = ("mutation($id:ID!,$in:CreateStationInput!){ updateStation(stationId:$id, input:$in)"
             "{ station { id } } }")
        res = self.client.execute(q, variables={"id": str(station.id), "in": VALID_INPUT},
                                  context=self.ctx(stranger))
        self.assertIsNotNone(res.get("errors"))

    def test_delete_is_soft(self):
        station = make_station(self.owner)
        q = "mutation($id:ID!){ deleteStation(stationId:$id){ ok } }"
        res = self.client.execute(q, variables={"id": str(station.id)}, context=self.ctx(self.owner))
        self.assertTrue(res["data"]["deleteStation"]["ok"])
        station.refresh_from_db()
        self.assertFalse(station.is_active)  # row preserved, just hidden


class ReviewValidation(StationTestBase):
    def setUp(self):
        super().setUp()
        self.station = make_station(self.owner)
        self.driver = make_user("driver")

    def _review(self, user, rating=5, comment="ok", station=None):
        q = ("mutation($s:ID!,$r:Int!,$c:String){ createReview(stationId:$s, rating:$r, comment:$c)"
             "{ review { id rating } } }")
        return self.client.execute(
            q, variables={"s": str((station or self.station).id), "r": rating, "c": comment},
            context=self.ctx(user),
        )

    def test_review_requires_completed_booking(self):
        res = self._review(self.driver)  # no booking yet
        self.assertIsNotNone(res.get("errors"))

    def test_valid_review_after_completed_booking(self):
        done_booking(self.driver, self.station)
        res = self._review(self.driver, rating=4)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["createReview"]["review"]["rating"], 4)

    def test_rating_out_of_range(self):
        done_booking(self.driver, self.station)
        self.assertIsNotNone(self._review(self.driver, rating=6).get("errors"))
        self.assertIsNotNone(self._review(self.driver, rating=0).get("errors"))

    def test_owner_cannot_review_own_station(self):
        done_booking(self.owner, self.station)
        res = self._review(self.owner)
        self.assertIsNotNone(res.get("errors"))

    def test_duplicate_review_rejected(self):
        done_booking(self.driver, self.station)
        self.assertIsNone(self._review(self.driver).get("errors"))
        self.assertIsNotNone(self._review(self.driver).get("errors"))

    def test_inactive_station_not_reviewable(self):
        done_booking(self.driver, self.station)
        self.station.is_active = False
        self.station.save()
        self.assertIsNotNone(self._review(self.driver).get("errors"))

    @override_settings(REVIEW_MAX_COMMENT_LENGTH=10)
    def test_comment_length_enforced(self):
        done_booking(self.driver, self.station)
        self.assertIsNotNone(self._review(self.driver, comment="x" * 50).get("errors"))

    def test_deactivated_user_cannot_review(self):
        dead = make_user("dead", is_active=False)
        done_booking(dead, self.station)
        self.assertIsNotNone(self._review(dead).get("errors"))

    def test_update_and_delete_require_ownership(self):
        done_booking(self.driver, self.station)
        self._review(self.driver, rating=3)
        review = Review.objects.get(user=self.driver, station=self.station)
        stranger = make_user("driver2")
        upd = ("mutation($id:ID!,$r:Int!){ updateReview(reviewId:$id, rating:$r){ review { id } } }")
        res = self.client.execute(upd, variables={"id": str(review.id), "r": 1}, context=self.ctx(stranger))
        self.assertIsNotNone(res.get("errors"))
        dele = "mutation($id:ID!){ deleteReview(reviewId:$id){ ok } }"
        res2 = self.client.execute(dele, variables={"id": str(review.id)}, context=self.ctx(stranger))
        self.assertIsNotNone(res2.get("errors"))


class FavoriteValidation(StationTestBase):
    def setUp(self):
        super().setUp()
        self.station = make_station(self.owner)
        self.user = make_user("driver")

    def _toggle(self, user, station=None):
        q = ("mutation($s:ID!){ toggleFavoriteStation(stationId:$s){ success message } }")
        return self.client.execute(q, variables={"s": str((station or self.station).id)},
                                   context=self.ctx(user))

    def test_toggle_adds_then_removes(self):
        r1 = self._toggle(self.user)
        self.assertTrue(r1["data"]["toggleFavoriteStation"]["success"])
        self.assertTrue(Favorite.objects.filter(user=self.user, station=self.station).exists())
        r2 = self._toggle(self.user)
        self.assertTrue(r2["data"]["toggleFavoriteStation"]["success"])
        self.assertFalse(Favorite.objects.filter(user=self.user, station=self.station).exists())

    def test_cannot_favorite_inactive_station(self):
        self.station.is_active = False
        self.station.save()
        res = self._toggle(self.user)
        self.assertFalse(res["data"]["toggleFavoriteStation"]["success"])

    def test_favorite_missing_station_is_safe(self):
        q = "mutation($s:ID!){ toggleFavoriteStation(stationId:$s){ success message } }"
        res = self.client.execute(q, variables={"s": "999999"}, context=self.ctx(self.user))
        self.assertFalse(res["data"]["toggleFavoriteStation"]["success"])

    def test_no_duplicate_favorites(self):
        Favorite.objects.create(user=self.user, station=self.station)
        # Toggling removes rather than creating a duplicate.
        self._toggle(self.user)
        self.assertEqual(Favorite.objects.filter(user=self.user, station=self.station).count(), 0)
