"""Administration tests (Sprint B1, Phases 2–5, 7).

Covers the owner approval lifecycle, the destructive-action interlocks, the
audit trail, and the paginated user directory.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.utils import timezone
from graphene.test import Client

from bookings.models import Booking
from ev_backend.schema import schema
from stations.models import Favorite, Review, Station

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
        availability="24/7", charger_type="CCS", num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )
    defaults.update(kw)
    return Station.objects.create(owner=owner, **defaults)


def run(query, user=None, ip=None, **variables):
    request = RequestFactory().post("/graphql/", REMOTE_ADDR=ip or "203.0.113.7")
    request.user = user or AnonymousUser()
    return Client(schema).execute(query, context=request, variables=variables or None)


class OwnerApprovalLifecycle(TestCase):
    """B1 Phase 2. Approval is a real state machine on `owner_status`."""

    def setUp(self):
        self.admin = make_user("admin1", role="admin")

    def test_registering_as_an_owner_creates_a_pending_account(self):
        res = run(
            """
            mutation { createUser(username: "newowner", email: "n@x.com", pin: "123456",
                                  isStationOwner: true) { user { id role ownerStatus isActive } } }
            """
        )
        self.assertIsNone(res.get("errors"))
        user = res["data"]["createUser"]["user"]
        self.assertEqual(user["role"], "station_owner")
        self.assertEqual(user["ownerStatus"], "pending")
        # Pending gates station management, NOT access: they can still sign in.
        self.assertTrue(user["isActive"])

    def test_registering_as_a_customer_has_no_approval_state(self):
        res = run(
            'mutation { createUser(username: "cust", email: "c@x.com", pin: "123456") '
            '{ user { role ownerStatus } } }'
        )
        self.assertIsNone(res.get("errors"))
        self.assertIsNone(res["data"]["createUser"]["user"]["ownerStatus"])

    def test_a_pending_owner_cannot_create_a_station(self):
        pending = make_user("pending1", role="station_owner", owner_status=User.OWNER_PENDING)
        res = run(
            """
            mutation { createStation(input: {name: "S", location: "L", latitude: 1.0,
                                             longitude: 1.0, pricePerKwh: 5}) { station { id } } }
            """,
            user=pending,
        )
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("awaiting approval", res["errors"][0]["message"])
        self.assertEqual(Station.objects.count(), 0)

    def test_a_rejected_owner_cannot_create_a_station(self):
        rejected = make_user("rej1", role="station_owner", owner_status=User.OWNER_REJECTED)
        res = run(
            """
            mutation { createStation(input: {name: "S", location: "L", latitude: 1.0,
                                             longitude: 1.0, pricePerKwh: 5}) { station { id } } }
            """,
            user=rejected,
        )
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("was not approved", res["errors"][0]["message"])

    def test_a_pending_owner_can_still_use_the_app_as_a_customer(self):
        pending = make_user("pending2", role="station_owner", owner_status=User.OWNER_PENDING)
        res = run("{ myBookingsPage { totalCount } me { username ownerStatus } }", user=pending)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["me"]["ownerStatus"], "pending")

    def test_approving_lets_the_owner_manage_stations(self):
        pending = make_user("pending3", role="station_owner", owner_status=User.OWNER_PENDING)
        res = run(
            "mutation($id: ID!) { approveStationOwner(userId: $id) { success user { ownerStatus } } }",
            user=self.admin, id=pending.id,
        )
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["approveStationOwner"]["user"]["ownerStatus"], "approved")

        pending.refresh_from_db()
        created = run(
            """
            mutation { createStation(input: {name: "S", location: "L", latitude: 1.0,
                                             longitude: 1.0, pricePerKwh: 5}) { station { id } } }
            """,
            user=pending,
        )
        self.assertIsNone(created.get("errors"))

    def test_approval_no_longer_touches_is_active(self):
        # The old mutation set is_active=True on an already-active account, which
        # is precisely why it did nothing.
        pending = make_user("pending4", role="station_owner", owner_status=User.OWNER_PENDING,
                            is_active=False)
        run("mutation($id: ID!) { approveStationOwner(userId: $id) { success } }",
            user=self.admin, id=pending.id)
        pending.refresh_from_db()
        self.assertEqual(pending.owner_status, User.OWNER_APPROVED)
        self.assertFalse(pending.is_active, "approval must not silently reactivate an account")

    def test_approval_is_idempotent(self):
        owner = make_user("owner9", role="station_owner")
        res = run("mutation($id: ID!) { approveStationOwner(userId: $id) { success } }",
                  user=self.admin, id=owner.id)
        self.assertIsNone(res.get("errors"))
        # Already approved: no second audit record for a state that did not change.
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.ACTION_OWNER_APPROVED).count(), 0,
        )

    def test_rejecting_an_owner_withdraws_station_management(self):
        owner = make_user("owner10", role="station_owner")
        res = run(
            'mutation($id: ID!) { rejectStationOwner(userId: $id, reason: "Unverified") '
            '{ success user { ownerStatus } } }',
            user=self.admin, id=owner.id,
        )
        self.assertIsNone(res.get("errors"))
        self.assertEqual(res["data"]["rejectStationOwner"]["user"]["ownerStatus"], "rejected")

        owner.refresh_from_db()
        self.assertTrue(owner.is_active, "rejection is not a ban")

    def test_only_station_owners_require_approval(self):
        customer = make_user("cust9")
        res = run("mutation($id: ID!) { approveStationOwner(userId: $id) { success } }",
                  user=self.admin, id=customer.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("Only station owners", res["errors"][0]["message"])

    def test_a_customer_cannot_approve_an_owner(self):
        pending = make_user("pending5", role="station_owner", owner_status=User.OWNER_PENDING)
        res = run("mutation($id: ID!) { approveStationOwner(userId: $id) { success } }",
                  user=make_user("cust10"), id=pending.id)
        self.assertIsNotNone(res.get("errors"))
        pending.refresh_from_db()
        self.assertEqual(pending.owner_status, User.OWNER_PENDING)

    def test_an_owner_cannot_approve_themselves(self):
        pending = make_user("pending6", role="station_owner", owner_status=User.OWNER_PENDING)
        res = run("mutation($id: ID!) { approveStationOwner(userId: $id) { success } }",
                  user=pending, id=pending.id)
        self.assertIsNotNone(res.get("errors"))
        pending.refresh_from_db()
        self.assertEqual(pending.owner_status, User.OWNER_PENDING)


class ActivationSafety(TestCase):
    """B1 Phase 3."""

    def setUp(self):
        self.admin = make_user("admin2", role="admin")
        self.other_admin = make_user("admin3", role="admin")
        self.customer = make_user("cust11")

    MUTATION = ("mutation($id: ID!, $a: Boolean!) { toggleUserActive(userId: $id, isActive: $a) "
                "{ user { id isActive } } }")

    def test_an_admin_cannot_deactivate_themselves(self):
        res = run(self.MUTATION, user=self.admin, id=self.admin.id, a=False)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("your own account", res["errors"][0]["message"])
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_the_last_active_admin_cannot_be_deactivated(self):
        self.other_admin.is_active = False
        self.other_admin.save()
        res = run(self.MUTATION, user=self.other_admin, id=self.admin.id, a=False)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("last active administrator", res["errors"][0]["message"])

    def test_an_admin_can_be_deactivated_while_another_remains(self):
        res = run(self.MUTATION, user=self.admin, id=self.other_admin.id, a=False)
        self.assertIsNone(res.get("errors"))
        self.assertFalse(res["data"]["toggleUserActive"]["user"]["isActive"])

    def test_deactivating_a_customer_is_recorded(self):
        run(self.MUTATION, user=self.admin, id=self.customer.id, a=False)
        entry = AuditLog.objects.get(action=AuditLog.ACTION_USER_DEACTIVATED)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target_id, str(self.customer.id))
        self.assertEqual(entry.target_label, "cust11")
        self.assertEqual(entry.ip, "203.0.113.7")

    def test_reactivating_is_recorded_separately(self):
        self.customer.is_active = False
        self.customer.save()
        run(self.MUTATION, user=self.admin, id=self.customer.id, a=True)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.ACTION_USER_ACTIVATED).exists())

    def test_a_no_op_writes_no_audit_record(self):
        run(self.MUTATION, user=self.admin, id=self.customer.id, a=True)  # already active
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_a_customer_cannot_deactivate_anyone(self):
        res = run(self.MUTATION, user=self.customer, id=self.other_admin.id, a=False)
        self.assertIsNotNone(res.get("errors"))
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_a_missing_user_is_reported_as_not_found(self):
        res = run(self.MUTATION, user=self.admin, id=999999, a=False)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("not found", res["errors"][0]["message"].lower())


class DeletionSafety(TestCase):
    """B1 Phase 3. The blast radius is computed before anything is destroyed."""

    def setUp(self):
        self.admin = make_user("admin4", role="admin")
        self.owner = make_user("owner11", role="station_owner")
        self.customer = make_user("cust12")
        self.station = make_station(self.owner)
        Booking.objects.create(
            user=self.customer, station=self.station, status="done",
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=2),
        )
        Review.objects.create(user=self.customer, station=self.station, rating=5)
        Favorite.objects.create(user=self.customer, station=self.station)

    PREVIEW = ("query($id: ID!) { userDeletionPreview(userId: $id) "
               "{ stations bookings reviews favorites otherUsersAffected } }")
    DELETE = ("mutation($id: ID!) { deleteUser(userId: $id) { ok "
              "summary { stations bookings reviews favorites otherUsersAffected } } }")

    def test_the_preview_counts_what_a_delete_would_destroy(self):
        res = run(self.PREVIEW, user=self.admin, id=self.owner.id)
        self.assertIsNone(res.get("errors"))
        summary = res["data"]["userDeletionPreview"]
        self.assertEqual(summary["stations"], 1)
        self.assertEqual(summary["bookings"], 1)
        self.assertEqual(summary["reviews"], 1)
        self.assertEqual(summary["favorites"], 1)
        # The number the console could never compute: deleting this owner erases
        # a DIFFERENT customer's history.
        self.assertEqual(summary["otherUsersAffected"], 1)

    def test_the_preview_changes_nothing(self):
        run(self.PREVIEW, user=self.admin, id=self.owner.id)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())
        self.assertEqual(Station.objects.count(), 1)

    def test_deleting_returns_the_same_summary_and_cascades(self):
        res = run(self.DELETE, user=self.admin, id=self.owner.id)
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["deleteUser"]["ok"])
        self.assertEqual(res["data"]["deleteUser"]["summary"]["otherUsersAffected"], 1)

        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())
        self.assertEqual(Station.objects.count(), 0)
        self.assertEqual(Booking.objects.count(), 0)
        # The customer themselves survives — only their history at that station goes.
        self.assertTrue(User.objects.filter(pk=self.customer.pk).exists())

    def test_an_admin_cannot_delete_themselves(self):
        res = run(self.DELETE, user=self.admin, id=self.admin.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("your own account", res["errors"][0]["message"])
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_an_admin_can_be_deleted_while_another_active_one_remains(self):
        second = make_user("admin5", role="admin")
        res = run(self.DELETE, user=second, id=self.admin.id)
        self.assertIsNone(res.get("errors"))
        self.assertFalse(User.objects.filter(pk=self.admin.pk).exists())

    def test_deleting_the_last_active_admin_is_refused(self):
        # The actor is deactivated, so `sole` is the platform's only way back in.
        # There is no API to create an admin — losing this one needs shell access.
        sole = make_user("admin6", role="admin")
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        res = run(self.DELETE, user=self.admin, id=sole.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertIn("last active administrator", res["errors"][0]["message"])
        self.assertTrue(User.objects.filter(pk=sole.pk).exists())

    def test_the_audit_record_survives_the_deletion_it_records(self):
        run(self.DELETE, user=self.admin, id=self.owner.id)
        entry = AuditLog.objects.get(action=AuditLog.ACTION_USER_DELETED)
        # The target is a snapshot, not a foreign key — an FK would have been
        # cascaded away by the very delete it is evidence of.
        self.assertEqual(entry.target_label, "owner11")
        self.assertEqual(entry.metadata["deleted"]["stations"], 1)
        self.assertEqual(entry.metadata["deleted"]["other_users_affected"], 1)

    def test_the_trail_outlives_the_admin_who_acted(self):
        run(self.DELETE, user=self.admin, id=self.owner.id)
        second = make_user("admin7", role="admin")
        run(self.DELETE, user=second, id=self.admin.id)

        entry = AuditLog.objects.filter(target_label="owner11").first()
        entry.refresh_from_db()
        self.assertIsNone(entry.actor, "actor is SET_NULL, not cascaded")
        self.assertEqual(entry.actor_username, "admin4", "who did it is preserved")

    def test_a_customer_cannot_delete_anyone(self):
        res = run(self.DELETE, user=self.customer, id=self.owner.id)
        self.assertIsNotNone(res.get("errors"))
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

    def test_a_customer_cannot_preview_a_deletion(self):
        res = run(self.PREVIEW, user=self.customer, id=self.owner.id)
        self.assertIsNotNone(res.get("errors"))


class UsersPage(TestCase):
    """B1 Phase 5."""

    def setUp(self):
        self.admin = make_user("zadmin", role="admin")
        for index in range(25):
            make_user(f"cust{index:02d}")
        make_user("pendingowner", role="station_owner", owner_status=User.OWNER_PENDING)
        make_user("approvedowner", role="station_owner")
        make_user("dormant", is_active=False)

    QUERY = """
        query($role: String, $search: String, $isActive: Boolean, $ownerStatus: String,
              $orderBy: String, $limit: Int, $offset: Int) {
          usersPage(role: $role, search: $search, isActive: $isActive,
                    ownerStatus: $ownerStatus, orderBy: $orderBy,
                    limit: $limit, offset: $offset) {
            items { id username role isActive ownerStatus }
            totalCount
            hasNext
          }
        }
    """

    def test_it_pages_with_a_total_and_a_next_flag(self):
        res = run(self.QUERY, user=self.admin, limit=10, offset=0)
        self.assertIsNone(res.get("errors"))
        page = res["data"]["usersPage"]
        self.assertEqual(len(page["items"]), 10)
        self.assertEqual(page["totalCount"], 29)
        self.assertTrue(page["hasNext"])

    def test_the_last_page_reports_no_next(self):
        res = run(self.QUERY, user=self.admin, limit=10, offset=20)
        self.assertFalse(res["data"]["usersPage"]["hasNext"])
        self.assertEqual(len(res["data"]["usersPage"]["items"]), 9)

    def test_paging_is_stable_across_pages(self):
        # Accounts created in the same instant share date_joined; without the
        # primary-key tie-break a row can repeat or vanish between pages.
        first = run(self.QUERY, user=self.admin, limit=10, offset=0)
        second = run(self.QUERY, user=self.admin, limit=10, offset=10)
        ids = [row["id"] for row in first["data"]["usersPage"]["items"]]
        ids += [row["id"] for row in second["data"]["usersPage"]["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_it_filters_by_role(self):
        res = run(self.QUERY, user=self.admin, role="station_owner")
        self.assertEqual(res["data"]["usersPage"]["totalCount"], 2)

    def test_it_filters_by_owner_status_to_give_a_pending_queue(self):
        res = run(self.QUERY, user=self.admin, role="station_owner", ownerStatus="pending")
        page = res["data"]["usersPage"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual(page["items"][0]["username"], "pendingowner")

    def test_it_filters_by_active_state(self):
        res = run(self.QUERY, user=self.admin, isActive=False)
        self.assertEqual(res["data"]["usersPage"]["totalCount"], 1)
        self.assertEqual(res["data"]["usersPage"]["items"][0]["username"], "dormant")

    def test_it_searches_username_and_email_case_insensitively(self):
        res = run(self.QUERY, user=self.admin, search="PENDINGOWNER")
        self.assertEqual(res["data"]["usersPage"]["totalCount"], 1)

        by_email = run(self.QUERY, user=self.admin, search="dormant@x.com")
        self.assertEqual(by_email["data"]["usersPage"]["totalCount"], 1)

    def test_search_combines_with_filters(self):
        res = run(self.QUERY, user=self.admin, role="station_owner", search="owner")
        self.assertEqual(res["data"]["usersPage"]["totalCount"], 2)

    def test_it_orders_by_username(self):
        res = run(self.QUERY, user=self.admin, orderBy="username", limit=1)
        self.assertEqual(res["data"]["usersPage"]["items"][0]["username"], "approvedowner")

    def test_an_unknown_order_falls_back_instead_of_erroring(self):
        # Ordering is an allow-list: a caller must never be able to sort by
        # `password` and read the hash out one comparison at a time.
        res = run(self.QUERY, user=self.admin, orderBy="password", limit=1)
        self.assertIsNone(res.get("errors"))

    def test_the_page_size_is_clamped_to_the_backend_maximum(self):
        res = run(self.QUERY, user=self.admin, limit=10_000)
        self.assertLessEqual(len(res["data"]["usersPage"]["items"]), 100)

    def test_a_customer_cannot_read_the_directory(self):
        res = run(self.QUERY, user=make_user("nosy"))
        self.assertIsNotNone(res.get("errors"))

    def test_anonymous_cannot_read_the_directory(self):
        res = run(self.QUERY)
        self.assertIsNotNone(res.get("errors"))

    def test_users_by_role_still_works_for_the_existing_client(self):
        res = run('query { usersByRole(role: "station_owner") { username ownerStatus } }',
                  user=self.admin)
        self.assertIsNone(res.get("errors"))
        self.assertEqual(len(res["data"]["usersByRole"]), 2)


class AuditLogQuery(TestCase):
    """B1 Phase 4."""

    def setUp(self):
        self.admin = make_user("admin8", role="admin")
        self.customer = make_user("cust13")
        run("mutation($id: ID!) { toggleUserActive(userId: $id, isActive: false) { user { id } } }",
            user=self.admin, id=self.customer.id)

    QUERY = """
        query($action: String, $targetId: String, $limit: Int, $offset: Int) {
          auditLogsPage(action: $action, targetId: $targetId, limit: $limit, offset: $offset) {
            items { action actorUsername targetType targetId targetLabel ip metadata createdAt }
            totalCount
            hasNext
          }
        }
    """

    def test_an_admin_can_read_the_trail(self):
        res = run(self.QUERY, user=self.admin)
        self.assertIsNone(res.get("errors"))
        page = res["data"]["auditLogsPage"]
        self.assertEqual(page["totalCount"], 1)
        entry = page["items"][0]
        self.assertEqual(entry["action"], "user_deactivated")
        self.assertEqual(entry["actorUsername"], "admin8")
        self.assertEqual(entry["targetLabel"], "cust13")

    def test_it_filters_by_action(self):
        res = run(self.QUERY, user=self.admin, action="user_deleted")
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 0)

    def test_it_filters_by_target(self):
        res = run(self.QUERY, user=self.admin, targetId=str(self.customer.id))
        self.assertEqual(res["data"]["auditLogsPage"]["totalCount"], 1)

    def test_a_customer_cannot_read_the_trail(self):
        res = run(self.QUERY, user=self.customer)
        self.assertIsNotNone(res.get("errors"))

    def test_anonymous_cannot_read_the_trail(self):
        res = run(self.QUERY)
        self.assertIsNotNone(res.get("errors"))
