"""Station administration and review moderation (Sprint B2.1).

The class that matters most here is `HidingAReviewRemovesItFromPublicReads`.
Everything else is ordinary CRUD coverage; that one is the difference between
moderation and a checkbox. A hidden review has to vanish from every public read
AND stop counting toward every rating — and there are four separate code paths
that put a rating in front of someone (the live list, the CACHED list, an owner's
own stations, a station's detail page). A test per path, because "I filtered the
obvious one" is exactly how this ships broken.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone
from graphene.test import Client

from accounts.models import AuditLog
from bookings.models import Booking
from ev_backend.schema import schema

from .models import Review, Station

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


def run(query, user=None, ip=None, **variables):
    request = RequestFactory().post("/graphql/", REMOTE_ADDR=ip or "203.0.113.7")
    request.user = user or AnonymousUser()
    return Client(schema).execute(query, context=request, variables=variables or None)


class HidingAReviewRemovesItFromPublicReads(TestCase):
    """Moderation that does not reach the public reads is not moderation."""

    def setUp(self):
        cache.clear()  # the public list is cached; don't inherit another test's
        self.admin = make_user("modadmin", role="admin")
        self.owner = make_user("modowner", role="station_owner")
        self.station = make_station(self.owner)
        # 5 and 1 average to 3.0. Hiding the 1 must move the average to 5.0 —
        # a value that cannot be produced by accident.
        self.kind = Review.objects.create(
            user=make_user("kind"), station=self.station, rating=5, comment="Great",
        )
        self.abusive = Review.objects.create(
            user=make_user("abusive"), station=self.station, rating=1, comment="SLUR",
        )

    def _hide(self):
        res = run(
            'mutation($id: ID!) { hideReview(reviewId: $id, reason: "Abuse") '
            '{ review { id isHidden } } }',
            user=self.admin, id=self.abusive.id,
        )
        self.assertIsNone(res.get("errors"))
        cache.clear()
        return res

    def test_the_station_average_and_count_ignore_a_hidden_review(self):
        before = run(
            "query($id: ID!) { stationById(stationId: $id) { averageRating numOfReviews } }",
            id=self.station.id,
        )["data"]["stationById"]
        self.assertEqual(before["averageRating"], 3.0)
        self.assertEqual(before["numOfReviews"], 2)

        self._hide()

        after = run(
            "query($id: ID!) { stationById(stationId: $id) { averageRating numOfReviews } }",
            id=self.station.id,
        )["data"]["stationById"]
        self.assertEqual(after["averageRating"], 5.0, "hidden review still moves the rating")
        self.assertEqual(after["numOfReviews"], 1)

    def test_a_hidden_review_disappears_from_the_public_review_list(self):
        self._hide()
        page = run(
            "query($id: ID!) { stationReviews(stationId: $id) { items { id comment } totalCount } }",
            id=self.station.id,
        )["data"]["stationReviews"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual([r["comment"] for r in page["items"]], ["Great"])

    def test_the_cached_public_station_list_drops_the_hidden_rating(self):
        # The most-read endpoint on the platform, and the one that would have
        # kept serving the hidden review's rating for the cache TTL if the
        # aggregate were not filtered at the source.
        run("{ stationList { stationId averageRate numOfRate } }")  # prime the cache
        self._hide()
        row = run("{ stationList { stationId averageRate numOfRate } }")["data"]["stationList"][0]
        self.assertEqual(row["averageRate"], 5.0)
        self.assertEqual(row["numOfRate"], 1)

    def test_the_paginated_public_station_list_drops_the_hidden_rating(self):
        self._hide()
        row = run("{ stationsPage { items { averageRate numOfRate } } }")
        self.assertEqual(row["data"]["stationsPage"]["items"][0]["averageRate"], 5.0)

    def test_filtering_by_minimum_rating_uses_the_moderated_average(self):
        # Pre-hide the station averages 3.0 and must not match minRating: 4.
        self.assertEqual(
            len(run("{ filterStations(minRating: 4) { stationId } }")["data"]["filterStations"]), 0,
        )
        self._hide()
        self.assertEqual(
            len(run("{ filterStations(minRating: 4) { stationId } }")["data"]["filterStations"]), 1,
        )

    def test_an_owners_own_station_list_uses_the_moderated_average(self):
        self._hide()
        row = run("{ myStations { averageRate numOfRate } }", user=self.owner)
        self.assertEqual(row["data"]["myStations"][0]["averageRate"], 5.0)

    def test_a_favourited_station_uses_the_moderated_average(self):
        customer = make_user("faver")
        run("mutation($id: ID!) { toggleFavoriteStation(stationId: $id) { success } }",
            user=customer, id=self.station.id)
        self._hide()
        row = run("{ myFavorites { items { averageRate } } }", user=customer)
        self.assertEqual(row["data"]["myFavorites"]["items"][0]["averageRate"], 5.0)

    def test_restoring_a_review_brings_it_back_everywhere(self):
        self._hide()
        res = run(
            "mutation($id: ID!) { restoreReview(reviewId: $id) { review { isHidden } } }",
            user=self.admin, id=self.abusive.id,
        )
        self.assertIsNone(res.get("errors"))
        self.assertFalse(res["data"]["restoreReview"]["review"]["isHidden"])
        cache.clear()

        detail = run(
            "query($id: ID!) { stationById(stationId: $id) { averageRating numOfReviews } }",
            id=self.station.id,
        )["data"]["stationById"]
        self.assertEqual(detail["averageRating"], 3.0)
        self.assertEqual(detail["numOfReviews"], 2)

    def test_hiding_does_not_let_the_author_post_a_replacement(self):
        # Otherwise moderation is trivially defeated: hide, re-post, repeat.
        #
        # The completed-booking gate would refuse this author anyway, which would
        # make the test pass for the wrong reason and prove nothing about
        # moderation. Give them the booking so the duplicate check is what
        # actually has to hold.
        Booking.objects.create(
            user=self.abusive.user, station=self.station, status="done",
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=2),
        )
        self._hide()
        res = run(
            'mutation($id: ID!) { createReview(stationId: $id, rating: 1, comment: "SLUR AGAIN") '
            '{ review { id } } }',
            user=self.abusive.user, id=self.station.id,
        )
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("already reviewed", res["errors"][0]["message"])

    def test_the_author_can_still_delete_their_own_hidden_review(self):
        # Hiding is moderation, not confiscation: their words remain theirs.
        self._hide()
        res = run("mutation($id: ID!) { deleteReview(reviewId: $id) { ok } }",
                  user=self.abusive.user, id=self.abusive.id)
        self.assertIsNone(res.get("errors"))
        self.assertFalse(Review.objects.filter(pk=self.abusive.pk).exists())

    def test_hiding_is_recorded(self):
        self._hide()
        entry = AuditLog.objects.get(action=AuditLog.ACTION_REVIEW_HIDDEN)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target_type, "review")
        self.assertEqual(entry.target_id, str(self.abusive.id))
        self.assertEqual(entry.metadata["reason"], "Abuse")
        self.assertEqual(entry.metadata["author_username"], "abusive")

    def test_hiding_twice_writes_one_record(self):
        self._hide()
        self._hide()
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.ACTION_REVIEW_HIDDEN).count(), 1,
            "a no-op must not claim a state change that did not happen",
        )


class ReviewModerationAuthorization(TestCase):
    def setUp(self):
        self.admin = make_user("modadmin2", role="admin")
        self.owner = make_user("modowner2", role="station_owner")
        self.customer = make_user("modcust2")
        self.station = make_station(self.owner)
        self.review = Review.objects.create(
            user=self.customer, station=self.station, rating=1, comment="Bad",
        )

    HIDE = "mutation($id: ID!) { hideReview(reviewId: $id) { review { id } } }"
    RESTORE = "mutation($id: ID!) { restoreReview(reviewId: $id) { review { id } } }"
    DELETE = ('mutation($id: ID!) { adminDeleteReview(reviewId: $id, reason: "Abuse") { ok } }')
    PAGE = "{ reviewsPage { items { id isHidden } totalCount } }"

    def test_anonymous_cannot_hide_restore_delete_or_list(self):
        for query in (self.HIDE, self.RESTORE, self.DELETE):
            res = run(query, id=self.review.id)
            self.assertIsNotNone(res.get("errors"))
        self.assertIsNotNone(run(self.PAGE).get("errors"))

    def test_a_customer_cannot_moderate(self):
        for query in (self.HIDE, self.RESTORE, self.DELETE):
            res = run(query, user=self.customer, id=self.review.id)
            self.assertIsNotNone(res.get("errors"))
        self.review.refresh_from_db()
        self.assertFalse(self.review.is_hidden)

    def test_a_review_author_cannot_hide_their_own_review(self):
        # They may delete it (that is `deleteReview`), but hiding is a
        # moderation state, not a privacy control.
        res = run(self.HIDE, user=self.customer, id=self.review.id)
        self.assertIsNotNone(res.get("errors"))

    def test_a_station_owner_cannot_moderate_reviews_of_their_own_station(self):
        # The obvious conflict of interest: an owner would hide every 1-star.
        for query in (self.HIDE, self.DELETE):
            res = run(query, user=self.owner, id=self.review.id)
            self.assertIsNotNone(res.get("errors"))
        self.review.refresh_from_db()
        self.assertFalse(self.review.is_hidden)
        self.assertTrue(Review.objects.filter(pk=self.review.pk).exists())

    def test_an_owner_cannot_read_the_moderation_queue(self):
        self.assertIsNotNone(run(self.PAGE, user=self.owner).get("errors"))

    def test_an_admin_can(self):
        res = run(self.HIDE, user=self.admin, id=self.review.id)
        self.assertIsNone(res.get("errors"))

    def test_a_missing_review_is_reported_as_not_found(self):
        res = run(self.HIDE, user=self.admin, id=999999)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("not found", res["errors"][0]["message"].lower())


class AdminReviewDeletion(TestCase):
    def setUp(self):
        self.admin = make_user("modadmin3", role="admin")
        self.owner = make_user("modowner3", role="station_owner")
        self.author = make_user("modcust3")
        self.station = make_station(self.owner)
        self.review = Review.objects.create(
            user=self.author, station=self.station, rating=1, comment="Defamatory",
        )

    def test_deleting_requires_a_reason(self):
        # `reason: String!` — the schema refuses this before a resolver runs.
        res = run("mutation($id: ID!) { adminDeleteReview(reviewId: $id) { ok } }",
                  user=self.admin, id=self.review.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertTrue(Review.objects.filter(pk=self.review.pk).exists())

    def test_a_blank_reason_is_refused_by_the_service(self):
        res = run('mutation($id: ID!) { adminDeleteReview(reviewId: $id, reason: "   ") { ok } }',
                  user=self.admin, id=self.review.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("reason is required", res["errors"][0]["message"])
        self.assertTrue(Review.objects.filter(pk=self.review.pk).exists())

    def test_deleting_destroys_the_review_and_snapshots_it(self):
        res = run('mutation($id: ID!) { adminDeleteReview(reviewId: $id, reason: "Defamation") { ok } }',
                  user=self.admin, id=self.review.id)
        self.assertIsNone(res.get("errors"))
        self.assertFalse(Review.objects.filter(pk=self.review.pk).exists())

        entry = AuditLog.objects.get(action=AuditLog.ACTION_REVIEW_DELETED)
        # The row is gone; the record has to carry what was destroyed, or nobody
        # can ever answer whether the deletion was justified.
        self.assertEqual(entry.metadata["comment"], "Defamatory")
        self.assertEqual(entry.metadata["author_username"], "modcust3")
        self.assertEqual(entry.metadata["reason"], "Defamation")
        self.assertEqual(entry.target_label, "Station")

    def test_the_customer_facing_delete_review_still_works_untouched(self):
        # B2.1 must not have broken the author-only mutation both clients call.
        res = run("mutation($id: ID!) { deleteReview(reviewId: $id) { ok } }",
                  user=self.author, id=self.review.id)
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["deleteReview"]["ok"])

    def test_delete_review_is_still_author_only(self):
        stranger = make_user("stranger3")
        res = run("mutation($id: ID!) { deleteReview(reviewId: $id) { ok } }",
                  user=stranger, id=self.review.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("your own review", res["errors"][0]["message"])

    def test_an_admin_deleting_via_the_customer_mutation_is_still_refused(self):
        # `deleteReview` is author-only regardless of role; admins use
        # `adminDeleteReview`, which is audited and demands a reason.
        res = run("mutation($id: ID!) { deleteReview(reviewId: $id) { ok } }",
                  user=self.admin, id=self.review.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertEqual(AuditLog.objects.count(), 0)


class ReviewsPageFiltering(TestCase):
    def setUp(self):
        self.admin = make_user("modadmin4", role="admin")
        self.owner_a = make_user("owner_a", role="station_owner")
        self.owner_b = make_user("owner_b", role="station_owner")
        self.station_a = make_station(self.owner_a, name="Alpha")
        self.station_b = make_station(self.owner_b, name="Beta")
        self.cust1 = make_user("rcust1")
        self.cust2 = make_user("rcust2")

        Review.objects.create(user=self.cust1, station=self.station_a, rating=5, comment="lovely")
        Review.objects.create(user=self.cust2, station=self.station_a, rating=1, comment="awful",
                              is_hidden=True)
        Review.objects.create(user=self.cust1, station=self.station_b, rating=3, comment="fine")

    QUERY = """
        query($stationId: ID, $ownerId: ID, $customerId: ID, $rating: Int,
              $isHidden: Boolean, $search: String, $orderBy: String,
              $limit: Int, $offset: Int) {
          reviewsPage(stationId: $stationId, ownerId: $ownerId, customerId: $customerId,
                      rating: $rating, isHidden: $isHidden, search: $search,
                      orderBy: $orderBy, limit: $limit, offset: $offset) {
            items { id rating comment isHidden user { username } station { id name } }
            totalCount
            hasNext
          }
        }
    """

    def test_the_moderation_queue_sees_hidden_reviews(self):
        # The whole point: the one endpoint that must NOT filter them out.
        res = run(self.QUERY, user=self.admin)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 3)

    def test_it_filters_to_only_hidden(self):
        res = run(self.QUERY, user=self.admin, isHidden=True)
        page = res["data"]["reviewsPage"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual(page["items"][0]["comment"], "awful")

    def test_it_filters_to_only_visible(self):
        res = run(self.QUERY, user=self.admin, isHidden=False)
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 2)

    def test_it_filters_by_station(self):
        res = run(self.QUERY, user=self.admin, stationId=self.station_a.id)
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 2)

    def test_it_filters_by_station_owner(self):
        res = run(self.QUERY, user=self.admin, ownerId=self.owner_b.id)
        page = res["data"]["reviewsPage"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual(page["items"][0]["station"]["name"], "Beta")

    def test_it_filters_by_customer(self):
        res = run(self.QUERY, user=self.admin, customerId=self.cust1.id)
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 2)

    def test_it_filters_by_rating(self):
        res = run(self.QUERY, user=self.admin, rating=5)
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 1)

    def test_it_searches_the_comment_text(self):
        res = run(self.QUERY, user=self.admin, search="AWFUL")
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 1)

    def test_it_searches_by_author_and_station(self):
        self.assertEqual(
            run(self.QUERY, user=self.admin, search="rcust2")["data"]["reviewsPage"]["totalCount"], 1,
        )
        self.assertEqual(
            run(self.QUERY, user=self.admin, search="Alpha")["data"]["reviewsPage"]["totalCount"], 2,
        )

    def test_it_orders_by_rating_within_the_allow_list(self):
        res = run(self.QUERY, user=self.admin, orderBy="rating_high")
        self.assertEqual(res["data"]["reviewsPage"]["items"][0]["rating"], 5)
        res = run(self.QUERY, user=self.admin, orderBy="rating_low")
        self.assertEqual(res["data"]["reviewsPage"]["items"][0]["rating"], 1)

    def test_an_unknown_order_falls_back_instead_of_erroring(self):
        res = run(self.QUERY, user=self.admin, orderBy="user__password")
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["reviewsPage"]["totalCount"], 3)

    def test_it_pages_with_a_total_and_a_next_flag(self):
        res = run(self.QUERY, user=self.admin, limit=2, offset=0)
        page = res["data"]["reviewsPage"]
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["totalCount"], 3)
        self.assertTrue(page["hasNext"])

        last = run(self.QUERY, user=self.admin, limit=2, offset=2)
        self.assertFalse(last["data"]["reviewsPage"]["hasNext"])

    def test_the_page_size_is_clamped(self):
        res = run(self.QUERY, user=self.admin, limit=10_000)
        self.assertLessEqual(len(res["data"]["reviewsPage"]["items"]), 100)

    def test_a_moderator_sees_which_station_a_review_belongs_to(self):
        # `ReviewType` deliberately omits `station`; a queue spanning stations is
        # useless without it, which is why AdminReviewType exists.
        res = run(self.QUERY, user=self.admin, stationId=self.station_b.id)
        self.assertEqual(res["data"]["reviewsPage"]["items"][0]["station"]["name"], "Beta")


class StationAdministration(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_user("stadmin", role="admin")
        self.owner = make_user("stowner", role="station_owner")
        self.other_owner = make_user("stowner2", role="station_owner")
        self.customer = make_user("stcust")
        self.live = make_station(self.owner, name="Live", location="Addis")
        self.dead = make_station(self.other_owner, name="Dead", location="Bahir",
                                 is_active=False, charger_type="CHAdeMO")

    QUERY = """
        query($search: String, $ownerId: ID, $isActive: Boolean, $chargerType: String,
              $minRating: Float, $orderBy: String, $limit: Int, $offset: Int) {
          stationsPageAdmin(search: $search, ownerId: $ownerId, isActive: $isActive,
                            chargerType: $chargerType, minRating: $minRating,
                            orderBy: $orderBy, limit: $limit, offset: $offset) {
            items { id name isActive owner { id username } }
            totalCount
            hasNext
          }
        }
    """

    def test_the_admin_view_sees_inactive_stations(self):
        # The public endpoint cannot: this is the whole reason it exists.
        res = run(self.QUERY, user=self.admin)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["stationsPageAdmin"]["totalCount"], 2)

        public_page = run("{ stationsPage { totalCount } }")
        self.assertEqual(public_page["data"]["stationsPage"]["totalCount"], 1)

    def test_it_filters_by_active_state(self):
        res = run(self.QUERY, user=self.admin, isActive=False)
        page = res["data"]["stationsPageAdmin"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual(page["items"][0]["name"], "Dead")

    def test_it_filters_by_owner(self):
        res = run(self.QUERY, user=self.admin, ownerId=self.owner.id)
        self.assertEqual(res["data"]["stationsPageAdmin"]["totalCount"], 1)

    def test_it_filters_by_charger_type(self):
        res = run(self.QUERY, user=self.admin, chargerType="CHAdeMO")
        self.assertEqual(res["data"]["stationsPageAdmin"]["totalCount"], 1)

    def test_it_searches_name_location_and_owner(self):
        for term, expected in (("Live", 1), ("Bahir", 1), ("stowner2", 1), ("stowner", 2)):
            res = run(self.QUERY, user=self.admin, search=term)
            self.assertEqual(
                res["data"]["stationsPageAdmin"]["totalCount"], expected, f"search={term}",
            )

    def test_it_orders_within_the_allow_list(self):
        res = run(self.QUERY, user=self.admin, orderBy="name", limit=1)
        self.assertEqual(res["data"]["stationsPageAdmin"]["items"][0]["name"], "Dead")

    def test_an_unknown_order_falls_back_instead_of_erroring(self):
        res = run(self.QUERY, user=self.admin, orderBy="owner__password")
        self.assertIsNone(res.get("errors"))

    def test_it_pages(self):
        res = run(self.QUERY, user=self.admin, limit=1, offset=0)
        self.assertTrue(res["data"]["stationsPageAdmin"]["hasNext"])
        self.assertEqual(res["data"]["stationsPageAdmin"]["totalCount"], 2)

    def test_the_admin_station_view_still_narrows_the_owner_to_an_identity(self):
        res = run(
            "{ stationsPageAdmin { items { owner { email } } } }", user=self.admin,
        )
        self.assertIsNotNone(res.get("errors"), "owner.email must not be reachable")

    def test_anonymous_customer_and_owner_are_all_refused(self):
        for user in (None, self.customer, self.owner):
            res = run(self.QUERY, user=user)
            self.assertIsNotNone(res.get("errors"))


class StationActivation(TestCase):
    """B1's known deviation: there was no way back from a soft delete."""

    def setUp(self):
        cache.clear()
        self.admin = make_user("actadmin", role="admin")
        self.owner = make_user("actowner", role="station_owner")
        self.customer = make_user("actcust")
        self.station = make_station(self.owner)

    ACTIVATE = ("mutation($id: ID!) { activateStation(stationId: $id) "
                "{ station { id isActive } } }")
    DEACTIVATE = ('mutation($id: ID!) { deactivateStation(stationId: $id, reason: "Fraud") '
                  '{ station { id isActive } } }')

    def test_an_admin_can_take_a_station_down(self):
        res = run(self.DEACTIVATE, user=self.admin, id=self.station.id)
        self.assertIsNone(res.get("errors"))
        self.assertFalse(res["data"]["deactivateStation"]["station"]["isActive"])
        cache.clear()
        self.assertEqual(run("{ stationsPage { totalCount } }")["data"]["stationsPage"]["totalCount"], 0)

    def test_a_deactivated_station_cannot_be_booked(self):
        run(self.DEACTIVATE, user=self.admin, id=self.station.id)
        res = run(
            "mutation($id: ID!, $s: DateTime!, $e: DateTime!) "
            "{ createBooking(stationId: $id, startTime: $s, endTime: $e) { booking { id } } }",
            user=self.customer, id=self.station.id,
            s=(timezone.now() + timedelta(days=1)).isoformat(),
            e=(timezone.now() + timedelta(days=1, hours=1)).isoformat(),
        )
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("not available", res["errors"][0]["message"])

    def test_an_admin_can_restore_a_station_the_owner_soft_deleted(self):
        # The exact gap B1 recorded: `deleteStation` clears `is_active` and
        # nothing could set it back without database access.
        run("mutation($id: ID!) { deleteStation(stationId: $id) { ok } }",
            user=self.owner, id=self.station.id)
        self.station.refresh_from_db()
        self.assertFalse(self.station.is_active)

        res = run(self.ACTIVATE, user=self.admin, id=self.station.id)
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["activateStation"]["station"]["isActive"])
        cache.clear()
        self.assertEqual(
            run("{ stationsPage { totalCount } }")["data"]["stationsPage"]["totalCount"], 1,
        )

    def test_both_directions_are_recorded_with_the_owner_and_reason(self):
        run(self.DEACTIVATE, user=self.admin, id=self.station.id)
        entry = AuditLog.objects.get(action=AuditLog.ACTION_STATION_DEACTIVATED)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target_type, "station")
        self.assertEqual(entry.target_label, "Station")
        self.assertEqual(entry.metadata["reason"], "Fraud")
        self.assertEqual(entry.metadata["owner_username"], "actowner")
        self.assertEqual(entry.ip, "203.0.113.7")

        run(self.ACTIVATE, user=self.admin, id=self.station.id)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.ACTION_STATION_ACTIVATED).exists(),
        )

    def test_a_no_op_writes_no_audit_record(self):
        run(self.ACTIVATE, user=self.admin, id=self.station.id)  # already active
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_deactivating_leaves_existing_bookings_alone(self):
        # Deliberate: cancelling other people's plans is a different decision
        # with a customer-visible consequence, and B2.1 was not asked to take it.
        booking = Booking.objects.create(
            user=self.customer, station=self.station, status="approved",
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=1),
        )
        run(self.DEACTIVATE, user=self.admin, id=self.station.id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "approved")

    def test_an_owner_cannot_activate_their_own_station(self):
        run("mutation($id: ID!) { deleteStation(stationId: $id) { ok } }",
            user=self.owner, id=self.station.id)
        res = run(self.ACTIVATE, user=self.owner, id=self.station.id)
        self.assertIsNotNone(res.get("errors"))
        self.station.refresh_from_db()
        self.assertFalse(self.station.is_active)

    def test_a_customer_and_anonymous_cannot_activate_or_deactivate(self):
        for user in (None, self.customer):
            for query in (self.ACTIVATE, self.DEACTIVATE):
                res = run(query, user=user, id=self.station.id)
                self.assertIsNotNone(res.get("errors"))
        self.station.refresh_from_db()
        self.assertTrue(self.station.is_active)

    def test_a_missing_station_is_reported_as_not_found(self):
        res = run(self.ACTIVATE, user=self.admin, id=999999)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("not found", res["errors"][0]["message"].lower())
