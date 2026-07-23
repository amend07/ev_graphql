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


# ── Validator unit tests (stations/validators.py) ────────────────────────────

from stations import validators as V  # noqa: E402


@override_settings(
    STATION_MAX_DESCRIPTION_LENGTH=20,
    STATION_MAX_PRICE_PER_KWH=100,
    STATION_MAX_POWER_KW=400,
    STATION_MAX_CHARGERS=10,
    REVIEW_MIN_RATING=1,
    REVIEW_MAX_RATING=5,
    REVIEW_MAX_COMMENT_LENGTH=20,
)
class ValidatorTests(TestCase):
    def _bad(self, fn, *args):
        with self.assertRaises(V.InvalidInput):
            fn(*args)

    def test_name_bounds(self):
        self._bad(V.validate_name, "")
        self._bad(V.validate_name, None)
        self._bad(V.validate_name, "x" * 101)
        V.validate_name("Fine name")  # ok

    def test_location_bounds(self):
        self._bad(V.validate_location, "   ")
        self._bad(V.validate_location, "x" * 256)
        V.validate_location("Somewhere")

    def test_description_too_long(self):
        self._bad(V.validate_description, "x" * 21)
        V.validate_description(None)          # optional
        V.validate_description("short")

    def test_latitude_and_longitude(self):
        self._bad(V.validate_latitude, None)
        self._bad(V.validate_latitude, 91.0)
        self._bad(V.validate_longitude, None)
        self._bad(V.validate_longitude, -181.0)
        V.validate_latitude(9.0)
        V.validate_longitude(38.0)

    def test_normalize_charger_type_aliases_and_passthrough(self):
        self.assertEqual(V.normalize_charger_type("Type 2"), "type2")
        self.assertEqual(V.normalize_charger_type("GB/T"), "gb_t")
        self.assertEqual(V.normalize_charger_type("Tesla"), "nacs")
        self.assertEqual(V.normalize_charger_type("j1772"), "type1")
        self.assertEqual(V.normalize_charger_type("banana"), "banana")  # unrecognised
        self.assertIsNone(V.normalize_charger_type(None))               # non-str early return
        self.assertEqual(V.normalize_charger_type(123), 123)

    def test_validate_charger_type(self):
        V.validate_charger_type("")           # optional, skipped
        V.validate_charger_type("ccs2")       # ok
        self._bad(V.validate_charger_type, "banana")

    def test_normalize_and_validate_charge_mode(self):
        self.assertEqual(V.normalize_charge_mode(" DC "), "dc")
        self.assertIsNone(V.normalize_charge_mode(None))
        V.validate_charge_mode("")            # optional
        V.validate_charge_mode("ac")          # ok
        self._bad(V.validate_charge_mode, "turbo")

    def test_validate_price(self):
        V.validate_price(None)                # optional
        V.validate_price(5.0)
        self._bad(V.validate_price, -1.0)
        self._bad(V.validate_price, 101.0)

    def test_validate_power(self):
        V.validate_power(None)
        V.validate_power(50.0)
        self._bad(V.validate_power, 0.0)
        self._bad(V.validate_power, 401.0)

    def test_validate_charger_count(self):
        V.validate_charger_count(None)
        V.validate_charger_count(3)
        self._bad(V.validate_charger_count, 0)
        self._bad(V.validate_charger_count, 11)

    def test_validate_rating_and_comment(self):
        self._bad(V.validate_rating, None)
        self._bad(V.validate_rating, 6)
        V.validate_rating(3)
        self._bad(V.validate_comment, "x" * 21)
        V.validate_comment(None)
        V.validate_comment("fine")

    def test_station_input_requires_price_on_create(self):
        self._bad(V.validate_station_input, {"name": "A", "location": "L",
                                             "latitude": 1.0, "longitude": 2.0})

    def test_station_input_full_create_and_partial_update(self):
        # A complete, valid create payload exercises every optional branch.
        V.validate_station_input({
            "name": "A", "location": "L", "latitude": 1.0, "longitude": 2.0,
            "price_per_kwh": 5.0, "description": "ok", "charger_type": "ccs",
            "charge_mode": "dc", "power_output_kw": 50.0, "num_of_charger": 2,
            "station_count": 1,
        })
        # Partial update: only provided fields are checked; a bad one still fails.
        self._bad(lambda d: V.validate_station_input(d, partial=True),
                  {"num_of_charger": 0})


# ── Schema branch coverage (stations/schema.py) ──────────────────────────────

CREATE_STATION_MODE = (
    "mutation($in:CreateStationInput!){ createStation(input:$in)"
    "{ station { id chargeMode } } }"
)
UPDATE_STATION = (
    "mutation($id:ID!,$in:CreateStationInput!){ updateStation(stationId:$id, input:$in)"
    "{ station { id name } } }"
)
STATION_BY_ID = (
    "query($id:ID!){ stationById(stationId:$id){ id isFavorite } }"
)
FILTER = """
query($ct:String,$cm:String,$noc:Int,$minR:Float,$maxR:Float,$minP:Float,
      $maxP:Float,$minPr:Float,$maxPr:Float,$avail:String){
  filterStations(chargerType:$ct, chargeMode:$cm, numOfCharger:$noc,
                 minRating:$minR, maxRating:$maxR, minPower:$minP, maxPower:$maxP,
                 minPrice:$minPr, maxPrice:$maxPr, availability:$avail){ stationId }
}
"""


class StationSchemaBranchTests(StationTestBase):
    def setUp(self):
        super().setUp()
        self.driver = make_user("driver")
        self.active = make_station(
            self.owner, name="Active", location="A", charger_type="ccs",
            charge_mode="dc", num_of_charger=2, power_output_kw=50.0, price_per_kwh=5,
        )
        self.inactive = make_station(
            self.owner, name="Inactive", location="B", charger_type="type2",
            charge_mode="ac", num_of_charger=1, power_output_kw=22.0,
            price_per_kwh=9, is_active=False,
        )

    def _filter(self, **vars):
        res = self.client.execute(FILTER, variables=vars, context=self.ctx(AnonymousUser()))
        self.assertIsNone(res.get("errors"), res.get("errors"))
        return res["data"]["filterStations"]

    def test_create_station_normalizes_charge_mode(self):
        payload = dict(VALID_INPUT)
        payload.update(name="Norm", location="Z", chargeMode="DC")
        res = self.client.execute(
            CREATE_STATION_MODE, variables={"in": payload}, context=self.ctx(self.owner))
        self.assertIsNone(res.get("errors"), res.get("errors"))
        self.assertEqual(res["data"]["createStation"]["station"]["chargeMode"], "dc")

    def test_update_station_applies_input(self):
        res = self.client.execute(
            UPDATE_STATION,
            variables={"id": str(self.active.id),
                       "in": dict(VALID_INPUT, name="Renamed", location="A")},
            context=self.ctx(self.owner),
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        self.active.refresh_from_db()
        self.assertEqual(self.active.name, "Renamed")

    def test_update_station_invalid_input_rejected(self):
        res = self.client.execute(
            UPDATE_STATION,
            variables={"id": str(self.active.id),
                       "in": dict(VALID_INPUT, latitude=999.0)},
            context=self.ctx(self.owner),
        )
        self.assertIsNotNone(res.get("errors"))

    def test_delete_station_not_found(self):
        q = "mutation($id:ID!){ deleteStation(stationId:$id){ ok } }"
        res = self.client.execute(q, variables={"id": "999999"}, context=self.ctx(self.owner))
        self.assertIsNotNone(res.get("errors"))

    def test_create_review_station_not_found(self):
        q = ("mutation($s:ID!,$r:Int!){ createReview(stationId:$s, rating:$r){ review { id } } }")
        res = self.client.execute(q, variables={"s": "999999", "r": 5}, context=self.ctx(self.driver))
        self.assertIsNotNone(res.get("errors"))

    def test_update_and_delete_review_not_found(self):
        upd = "mutation($id:ID!,$r:Int!){ updateReview(reviewId:$id, rating:$r){ review { id } } }"
        self.assertIsNotNone(
            self.client.execute(upd, variables={"id": "999999", "r": 3},
                                context=self.ctx(self.driver)).get("errors"))
        dele = "mutation($id:ID!){ deleteReview(reviewId:$id){ ok } }"
        self.assertIsNotNone(
            self.client.execute(dele, variables={"id": "999999"},
                                context=self.ctx(self.driver)).get("errors"))

    def test_update_review_changes_rating_and_comment(self):
        done_booking(self.driver, self.active)
        Review.objects.create(user=self.driver, station=self.active, rating=3, comment="meh")
        review = Review.objects.get(user=self.driver, station=self.active)
        upd = ("mutation($id:ID!,$r:Int!,$c:String){ updateReview(reviewId:$id, rating:$r, comment:$c)"
               "{ review { id rating } } }")
        res = self.client.execute(
            upd, variables={"id": str(review.id), "r": 5, "c": "better"},
            context=self.ctx(self.driver))
        self.assertIsNone(res.get("errors"), res.get("errors"))
        review.refresh_from_db()
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.comment, "better")

    def test_station_by_id_not_found(self):
        res = self.client.execute(STATION_BY_ID, variables={"id": "999999"},
                                  context=self.ctx(AnonymousUser()))
        self.assertIsNotNone(res.get("errors"))

    def test_is_favorite_reflects_authenticated_user(self):
        Favorite.objects.create(user=self.driver, station=self.active)
        res = self.client.execute(STATION_BY_ID, variables={"id": str(self.active.id)},
                                  context=self.ctx(self.driver))
        self.assertTrue(res["data"]["stationById"]["isFavorite"])
        # Anonymous callers always see False.
        res2 = self.client.execute(STATION_BY_ID, variables={"id": str(self.active.id)},
                                   context=self.ctx(AnonymousUser()))
        self.assertFalse(res2["data"]["stationById"]["isFavorite"])

    def test_filter_by_charger_type_and_mode(self):
        rows = self._filter(ct="ccs")
        self.assertEqual({r["stationId"] for r in rows}, {str(self.active.id)})
        rows = self._filter(cm="dc")
        self.assertEqual({r["stationId"] for r in rows}, {str(self.active.id)})

    def test_filter_by_num_power_and_price_bounds(self):
        self.assertTrue(self._filter(noc=2))
        self.assertTrue(self._filter(minP=40.0, maxP=60.0))
        self.assertTrue(self._filter(minPr=1.0, maxPr=8.0))

    def test_filter_by_rating_bounds(self):
        # No ratings yet, so average is 0.0 — a min above 0 excludes everything.
        self.assertEqual(self._filter(minR=1.0), [])
        self.assertTrue(self._filter(maxR=5.0))

    def test_availability_scopes(self):
        # available -> active only; occupied -> inactive only; all -> both.
        available = {r["stationId"] for r in self._filter(avail="available")}
        self.assertIn(str(self.active.id), available)
        self.assertNotIn(str(self.inactive.id), available)

        occupied = {r["stationId"] for r in self._filter(avail="occupied")}
        self.assertEqual(occupied, {str(self.inactive.id)})

        every = {r["stationId"] for r in self._filter(avail="all")}
        self.assertIn(str(self.active.id), every)
        self.assertIn(str(self.inactive.id), every)

    def test_station_list_marks_favorites_for_authenticated_user(self):
        from stations.cache import get_public_station_rows  # noqa: F401
        Favorite.objects.create(user=self.driver, station=self.active)
        q = "query{ stationList { stationId isFavorite } }"
        res = self.client.execute(q, context=self.ctx(self.driver))
        self.assertIsNone(res.get("errors"), res.get("errors"))
        favs = {r["stationId"]: r["isFavorite"] for r in res["data"]["stationList"]}
        self.assertTrue(favs.get(str(self.active.id)))


class StationAdminFilterTests(StationTestBase):
    def test_stations_page_admin_min_rating_filter(self):
        admin = make_user("admin", role="admin")
        make_station(self.owner, name="S1", location="L1")
        q = ("query($r:Float){ stationsPageAdmin(minRating:$r){ totalCount items { name } } }")
        res = self.client.execute(q, variables={"r": 1.0}, context=self.ctx(admin))
        self.assertIsNone(res.get("errors"), res.get("errors"))
        # No reviews -> average_rate 0 -> nothing meets minRating 1.
        self.assertEqual(res["data"]["stationsPageAdmin"]["totalCount"], 0)


class ImageUploadValidationTests(TestCase):
    """Station image uploads must be real, bounded raster images (W9 hardening)."""

    def _png(self, name="a.png", content_type="image/png"):
        import io
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
        return SimpleUploadedFile(name, buf.getvalue(), content_type=content_type)

    def test_valid_png_is_accepted(self):
        from stations.validators import validate_image
        validate_image(self._png())  # must not raise

    def test_non_image_payload_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from stations.validators import validate_image, InvalidInput
        # A script masquerading as a png (content-type spoof).
        evil = SimpleUploadedFile(
            "x.png", b"<script>alert(1)</script>", content_type="image/png")
        with self.assertRaises(InvalidInput):
            validate_image(evil)

    def test_disallowed_content_type_is_rejected(self):
        from stations.validators import validate_image, InvalidInput
        with self.assertRaises(InvalidInput):
            validate_image(self._png(name="x.svg", content_type="image/svg+xml"))

    def test_oversized_image_is_rejected(self):
        from django.test import override_settings
        from stations.validators import validate_image, InvalidInput
        with override_settings(STATION_MAX_IMAGE_BYTES=10):
            with self.assertRaises(InvalidInput):
                validate_image(self._png())
