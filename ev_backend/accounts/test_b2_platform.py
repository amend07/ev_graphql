"""Platform administration APIs (Sprint B2.1).

Covers the completed owner-approval lifecycle (who decided, when, and why), the
`dashboardSummary` aggregates, and the expanded audit trail filters.

The dashboard tests deliberately assert exact numbers against a fixture built one
row at a time. A dashboard that is merely *plausible* is worse than no dashboard:
nobody double-checks a number on a summary screen, so a wrong one is believed.
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

from .models import AuditLog

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


def run(query, user=None, ip=None, **variables):
    request = RequestFactory().post("/graphql/", REMOTE_ADDR=ip or "203.0.113.7")
    request.user = user or AnonymousUser()
    return Client(schema).execute(query, context=request, variables=variables or None)


class OwnerDecisionProvenance(TestCase):
    """B1 shipped the state machine; B2.1 records who decided and why."""

    def setUp(self):
        self.admin = make_user("padmin", role="admin")
        self.pending = make_user("ppending", role="station_owner",
                                 owner_status=User.OWNER_PENDING)

    APPROVE = ("mutation($id: ID!) { approveStationOwner(userId: $id) "
               "{ user { ownerStatus reviewedAt rejectionReason reviewer { username } } } }")
    REJECT = ("mutation($id: ID!, $r: String!) { rejectStationOwner(userId: $id, reason: $r) "
              "{ user { ownerStatus reviewedAt rejectionReason reviewer { id username } } } }")

    def test_approving_records_the_reviewer_and_the_time(self):
        res = run(self.APPROVE, user=self.admin, id=self.pending.id)
        self.assertIsNone(res.get("errors"))
        user = res["data"]["approveStationOwner"]["user"]
        self.assertEqual(user["ownerStatus"], "approved")
        self.assertEqual(user["reviewer"]["username"], "padmin")
        self.assertIsNotNone(user["reviewedAt"])

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.reviewer, self.admin)

    def test_rejecting_records_the_reason_the_reviewer_and_the_time(self):
        res = run(self.REJECT, user=self.admin, id=self.pending.id, r="Documents unverifiable")
        self.assertIsNone(res.get("errors"))
        user = res["data"]["rejectStationOwner"]["user"]
        self.assertEqual(user["ownerStatus"], "rejected")
        self.assertEqual(user["rejectionReason"], "Documents unverifiable")
        self.assertEqual(user["reviewer"]["username"], "padmin")
        self.assertIsNotNone(user["reviewedAt"])

    def test_a_rejection_reason_is_mandatory_at_the_schema(self):
        # `reason: String!` — this is refused before any resolver runs.
        res = run("mutation($id: ID!) { rejectStationOwner(userId: $id) { success } }",
                  user=self.admin, id=self.pending.id)
        self.assertIsNotNone(res.get("errors"))
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.owner_status, "pending")

    def test_a_blank_rejection_reason_is_refused_by_the_service(self):
        # `String!` stops null, not "   ". The service is where the rule lives,
        # so it holds for every caller and not just this one.
        res = run(self.REJECT, user=self.admin, id=self.pending.id, r="   ")
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("reason is required", res["errors"][0]["message"])
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.owner_status, "pending")
        self.assertEqual(self.pending.rejection_reason, "")

    def test_the_rejected_owner_can_read_their_own_reason(self):
        # The point of storing it on the record rather than only in the audit
        # log, which they cannot read.
        run(self.REJECT, user=self.admin, id=self.pending.id, r="Documents unverifiable")
        self.pending.refresh_from_db()
        res = run("{ me { ownerStatus rejectionReason reviewer { username } } }",
                  user=self.pending)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["me"]["rejectionReason"], "Documents unverifiable")

    def test_a_rejected_owner_cannot_read_the_deciding_admins_email(self):
        # `reviewer` is PublicUserType. Auto-conversion would have made it a full
        # UserType and handed the admin's address to the applicant.
        run(self.REJECT, user=self.admin, id=self.pending.id, r="No")
        self.pending.refresh_from_db()
        res = run("{ me { reviewer { email } } }", user=self.pending)
        self.assertIsNotNone(res.get("errors"))

    def test_approving_after_a_rejection_clears_the_stale_reason(self):
        run(self.REJECT, user=self.admin, id=self.pending.id, r="Documents unverifiable")
        self.pending.refresh_from_db()
        res = run(self.APPROVE, user=self.admin, id=self.pending.id)
        user = res["data"]["approveStationOwner"]["user"]
        self.assertEqual(user["ownerStatus"], "approved")
        self.assertEqual(
            user["rejectionReason"], "",
            "an approved owner must not still carry why they were once rejected",
        )

    def test_the_reason_reaches_the_audit_trail(self):
        run(self.REJECT, user=self.admin, id=self.pending.id, r="Documents unverifiable")
        entry = AuditLog.objects.get(action=AuditLog.ACTION_OWNER_REJECTED)
        self.assertEqual(entry.metadata["reason"], "Documents unverifiable")
        self.assertEqual(entry.metadata["previous_status"], "pending")

    def test_the_decision_outlives_the_admin_who_took_it(self):
        # `reviewer` is SET_NULL, like AuditLog.actor: deleting the admin must
        # not erase the fact that the owner was rejected, or why.
        run(self.REJECT, user=self.admin, id=self.pending.id, r="Documents unverifiable")
        second = make_user("padmin2", role="admin")
        run("mutation($id: ID!) { deleteUser(userId: $id) { ok } }",
            user=second, id=self.admin.id)

        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.reviewer)
        self.assertEqual(self.pending.owner_status, "rejected")
        self.assertEqual(self.pending.rejection_reason, "Documents unverifiable")

    def test_the_pending_queue_is_served_by_users_page(self):
        # No bespoke pendingOwners query: usersPage already filters this exactly,
        # and a second endpoint answering the same question is a second thing to
        # keep correct.
        res = run(
            'query { usersPage(role: "station_owner", ownerStatus: "pending") '
            '{ items { username } totalCount } }',
            user=self.admin,
        )
        self.assertEqual(res["data"]["usersPage"]["totalCount"], 1)
        self.assertEqual(res["data"]["usersPage"]["items"][0]["username"], "ppending")

    def test_rejected_owners_are_listable_the_same_way(self):
        run(self.REJECT, user=self.admin, id=self.pending.id, r="No")
        res = run(
            'query { usersPage(role: "station_owner", ownerStatus: "rejected") '
            '{ totalCount } }',
            user=self.admin,
        )
        self.assertEqual(res["data"]["usersPage"]["totalCount"], 1)


class DashboardSummary(TestCase):
    """Every number counted from a fixture built one row at a time."""

    QUERY = """
        query($newestLimit: Int) {
          dashboardSummary(newestLimit: $newestLimit) {
            customers owners pendingOwners rejectedOwners
            stations activeStations inactiveStations
            bookingsToday bookingsThisWeek bookingsThisMonth
            reviews averageRating
            newestUsers { id username }
            newestStations { id name }
          }
        }
    """

    def setUp(self):
        self.admin = make_user("dadmin", role="admin")
        # 3 customers, 1 of whom is dormant.
        self.c1 = make_user("dcust1")
        self.c2 = make_user("dcust2")
        self.c3 = make_user("dcust3", is_active=False)
        # 4 owners: 2 approved, 1 pending, 1 rejected.
        self.o1 = make_user("downer1", role="station_owner")
        self.o2 = make_user("downer2", role="station_owner")
        make_user("downer3", role="station_owner", owner_status=User.OWNER_PENDING)
        make_user("downer4", role="station_owner", owner_status=User.OWNER_REJECTED)
        # 3 stations: 2 active, 1 not.
        self.s1 = make_station(self.o1, name="S1")
        self.s2 = make_station(self.o1, name="S2")
        self.s3 = make_station(self.o2, name="S3", is_active=False)

    def _book(self, when, user=None):
        return Booking.objects.create(
            user=user or self.c1, station=self.s1, status="pending",
            start_time=when + timedelta(days=30),
            end_time=when + timedelta(days=30, hours=1),
        )

    def _summary(self, **variables):
        res = run(self.QUERY, user=self.admin, **variables)
        self.assertIsNone(res.get("errors"))
        return res["data"]["dashboardSummary"]

    def test_it_counts_users_by_role_and_owner_status(self):
        data = self._summary()
        self.assertEqual(data["customers"], 3, "the admin is not a customer")
        self.assertEqual(data["owners"], 4)
        self.assertEqual(data["pendingOwners"], 1)
        self.assertEqual(data["rejectedOwners"], 1)

    def test_it_counts_stations_by_state(self):
        data = self._summary()
        self.assertEqual(data["stations"], 3)
        self.assertEqual(data["activeStations"], 2)
        self.assertEqual(data["inactiveStations"], 1)
        self.assertEqual(
            data["activeStations"] + data["inactiveStations"], data["stations"],
        )

    def test_it_counts_visible_reviews_and_their_average(self):
        Review.objects.create(user=self.c1, station=self.s1, rating=5)
        Review.objects.create(user=self.c2, station=self.s1, rating=1)
        data = self._summary()
        self.assertEqual(data["reviews"], 2)
        self.assertEqual(data["averageRating"], 3.0)

    def test_a_hidden_review_leaves_the_platform_average(self):
        # Moderation has to reach the dashboard too, or the platform-wide number
        # still carries content nobody is allowed to read.
        Review.objects.create(user=self.c1, station=self.s1, rating=5)
        Review.objects.create(user=self.c2, station=self.s1, rating=1, is_hidden=True)
        data = self._summary()
        self.assertEqual(data["reviews"], 1)
        self.assertEqual(data["averageRating"], 5.0)

    def test_no_reviews_reports_zero_not_null(self):
        data = self._summary()
        self.assertEqual(data["reviews"], 0)
        self.assertEqual(data["averageRating"], 0.0)

    def test_the_booking_windows_nest_correctly(self):
        now = timezone.now()
        self._book(now)                              # today
        self._book(now)                              # today
        booked_earlier = self._book(now)
        # created_at is auto_now_add, so move it explicitly to land in an older
        # window. Anything inside today's window must also be inside the week's.
        Booking.objects.filter(pk=booked_earlier.pk).update(
            created_at=now - timedelta(days=40),
        )

        data = self._summary()
        self.assertEqual(data["bookingsToday"], 2)
        self.assertGreaterEqual(data["bookingsThisWeek"], data["bookingsToday"])
        self.assertGreaterEqual(data["bookingsThisMonth"], data["bookingsThisWeek"])
        # The 40-day-old booking is outside every window but still exists.
        self.assertEqual(Booking.objects.count(), 3)
        self.assertEqual(data["bookingsThisMonth"], 2)

    def test_it_counts_bookings_created_today_not_bookings_starting_today(self):
        # The documented contract, and the one that could silently differ.
        now = timezone.now()
        Booking.objects.create(
            user=self.c1, station=self.s1, status="pending",
            start_time=now + timedelta(days=200),
            end_time=now + timedelta(days=200, hours=1),
        )
        self.assertEqual(
            self._summary()["bookingsToday"], 1,
            "a booking made today counts today even though its slot is months away",
        )

    def test_newest_users_and_stations_are_most_recent_first(self):
        data = self._summary()
        self.assertEqual(len(data["newestUsers"]), 5)
        self.assertEqual(data["newestUsers"][0]["username"], "downer4", "most recent first")
        self.assertEqual(len(data["newestStations"]), 3)
        self.assertEqual(data["newestStations"][0]["name"], "S3")

    def test_the_newest_limit_is_honoured_and_clamped(self):
        self.assertEqual(len(self._summary(newestLimit=2)["newestUsers"]), 2)
        # Clamped, not obeyed: this is a summary, not a directory. usersPage is.
        self.assertLessEqual(len(self._summary(newestLimit=10_000)["newestUsers"]), 20)

    def test_it_answers_in_a_bounded_number_of_queries(self):
        # The rule this endpoint exists to honour: aggregates, not row-fetching.
        # Counting in Python would scale the query count (or the memory) with the
        # platform, on the one page whose job is to report that it is growing.
        with self.assertNumQueries(6):
            run(
                """
                { dashboardSummary { customers owners pendingOwners rejectedOwners
                    stations activeStations inactiveStations
                    bookingsToday bookingsThisWeek bookingsThisMonth
                    reviews averageRating } }
                """,
                user=self.admin,
            )

    def test_it_publishes_no_money_or_fabricated_analytics(self):
        # `Booking` carries no price and no payment reference. Any revenue number
        # here would have to be invented, and an invented number on a dashboard
        # is indistinguishable from a real one.
        fields = set(schema.graphql_schema.type_map["DashboardSummaryType"].fields)
        for invented in ("revenue", "totalRevenue", "earnings", "growthRate",
                         "conversionRate", "utilisation", "utilization", "mrr"):
            self.assertNotIn(invented, fields)

    def test_anonymous_customer_and_owner_are_all_refused(self):
        for user in (None, self.c1, self.o1):
            res = run(self.QUERY, user=user)
            self.assertIsNotNone(res.get("errors"))


class AuditLogFilters(TestCase):
    """B2.1 filter expansion. Purely additive over B1's action/targetId."""

    def setUp(self):
        self.admin = make_user("aadmin", role="admin")
        self.admin2 = make_user("aadmin2", role="admin")
        self.customer = make_user("acust")
        self.owner = make_user("aowner", role="station_owner")
        self.station = make_station(self.owner)
        self.review = Review.objects.create(
            user=self.customer, station=self.station, rating=1, comment="bad",
        )

        # A trail spanning two actors and three target types.
        run("mutation($id: ID!) { toggleUserActive(userId: $id, isActive: false) { user { id } } }",
            user=self.admin, id=self.customer.id)
        run('mutation($id: ID!) { deactivateStation(stationId: $id, reason: "x") { station { id } } }',
            user=self.admin2, id=self.station.id)
        run('mutation($id: ID!) { hideReview(reviewId: $id, reason: "x") { review { id } } }',
            user=self.admin2, id=self.review.id)

    QUERY = """
        query($actorId: ID, $action: String, $targetType: String, $targetId: String,
              $dateFrom: DateTime, $dateTo: DateTime, $orderBy: String,
              $limit: Int, $offset: Int) {
          auditLogsPage(actorId: $actorId, action: $action, targetType: $targetType,
                        targetId: $targetId, dateFrom: $dateFrom, dateTo: $dateTo,
                        orderBy: $orderBy, limit: $limit, offset: $offset) {
            items { id action actorUsername targetType targetId targetLabel createdAt }
            totalCount
            hasNext
          }
        }
    """

    def test_the_trail_records_all_three_action_families(self):
        res = run(self.QUERY, user=self.admin)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 3)

    def test_it_filters_by_actor(self):
        res = run(self.QUERY, user=self.admin, actorId=self.admin2.id)
        page = res["data"]["auditLogsPage"]
        self.assertEqual(page["totalCount"], 2)
        self.assertEqual({e["actorUsername"] for e in page["items"]}, {"aadmin2"})

    def test_it_filters_by_target_type(self):
        for target_type, expected in (("user", 1), ("station", 1), ("review", 1)):
            res = run(self.QUERY, user=self.admin, targetType=target_type)
            self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], expected, target_type)

    def test_it_filters_by_action(self):
        res = run(self.QUERY, user=self.admin, action="review_hidden")
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 1)

    def test_it_filters_by_target_id(self):
        res = run(self.QUERY, user=self.admin, targetId=str(self.station.id),
                  targetType="station")
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 1)

    def test_it_filters_by_date_range(self):
        now = timezone.now()
        self.assertEqual(
            run(self.QUERY, user=self.admin,
                dateFrom=(now - timedelta(hours=1)).isoformat())["data"]["auditLogsPage"]["totalCount"],
            3,
        )
        self.assertEqual(
            run(self.QUERY, user=self.admin,
                dateFrom=(now + timedelta(hours=1)).isoformat())["data"]["auditLogsPage"]["totalCount"],
            0,
        )
        self.assertEqual(
            run(self.QUERY, user=self.admin,
                dateTo=(now - timedelta(hours=1)).isoformat())["data"]["auditLogsPage"]["totalCount"],
            0,
        )

    def test_filters_combine(self):
        res = run(self.QUERY, user=self.admin, actorId=self.admin2.id, targetType="review")
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 1)

    def test_it_orders_within_the_allow_list(self):
        res = run(self.QUERY, user=self.admin, orderBy="oldest")
        self.assertEqual(res["data"]["auditLogsPage"]["items"][0]["action"], "user_deactivated")
        res = run(self.QUERY, user=self.admin, orderBy="newest")
        self.assertEqual(res["data"]["auditLogsPage"]["items"][0]["action"], "review_hidden")

    def test_an_unknown_order_falls_back_instead_of_erroring(self):
        res = run(self.QUERY, user=self.admin, orderBy="actor__password")
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 3)

    def test_it_pages_stably(self):
        first = run(self.QUERY, user=self.admin, limit=2, offset=0)["data"]["auditLogsPage"]
        second = run(self.QUERY, user=self.admin, limit=2, offset=2)["data"]["auditLogsPage"]
        self.assertTrue(first["hasNext"])
        self.assertFalse(second["hasNext"])
        ids = [e["id"] for e in first["items"]] + [e["id"] for e in second["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_b1_call_shape_still_works_unchanged(self):
        # Every new argument is optional: a B1 client's document must still run.
        res = run(
            'query { auditLogsPage(action: "user_deactivated", targetId: "%s") '
            '{ items { action } totalCount } }' % self.customer.id,
            user=self.admin,
        )
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 1)

    def test_a_customer_and_anonymous_cannot_read_the_trail(self):
        for user in (None, self.customer, self.owner):
            self.assertIsNotNone(run(self.QUERY, user=user).get("errors"))
