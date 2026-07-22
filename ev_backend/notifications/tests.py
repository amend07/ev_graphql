"""Notifications: API (query/mark) and the domain triggers that create them."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from graphene.test import Client

from accounts.administration import approve_owner, reject_owner
from accounts.services import create_account
from bookings.models import Booking
from ev_backend.schema import schema
from notifications.models import Notification, NotificationPreference
from notifications.service import notify, notify_admins
from stations.models import Station

User = get_user_model()

MY_NOTIFS = """
query($limit:Int,$offset:Int,$unread:Boolean){
  myNotificationsPage(limit:$limit, offset:$offset, unreadOnly:$unread){
    totalCount hasNext
    items { id type title body isRead createdAt relatedId }
  }
}
"""
UNREAD = "query{ myUnreadNotificationCount }"
MARK = """
mutation($id:ID!){
  markNotificationRead(notificationId:$id){ ok notification { id isRead } }
}
"""
MARK_ALL = "mutation{ markAllNotificationsRead{ ok updated } }"
PREFS = """
query{
  myNotificationPreferences{ enabled booking review station system }
}
"""
UPDATE_PREFS = """
mutation($enabled:Boolean,$booking:Boolean,$review:Boolean,$station:Boolean,$system:Boolean){
  updateNotificationPreferences(
    enabled:$enabled, booking:$booking, review:$review, station:$station, system:$system
  ){ ok preferences{ enabled booking review station system } }
}
"""

CREATE_BOOKING = """
mutation($s:ID!,$a:DateTime!,$b:DateTime!){
  createBooking(stationId:$s, startTime:$a, endTime:$b){ booking { id status } }
}
"""
UPDATE_BOOKING = """
mutation($id:ID!,$s:String!){
  updateBookingStatus(bookingId:$id, status:$s){ booking { status } }
}
"""
CREATE_REVIEW = """
mutation($s:ID!,$r:Int!,$c:String){
  createReview(stationId:$s, rating:$r, comment:$c){ review { id } }
}
"""


def make_user(username, role="user", is_active=True, owner_status=None):
    if role == "station_owner" and owner_status is None:
        owner_status = User.OWNER_APPROVED
    return User.objects.create_user(
        username=username, email=f"{username}@x.com", password="123456",
        role=role, is_active=is_active, owner_status=owner_status,
    )


def make_station(owner, **kw):
    defaults = dict(
        name="Station", location="Loc", latitude=1.0, longitude=1.0,
        availability="24/7", charger_type="CCS", num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )
    defaults.update(kw)
    return Station.objects.create(owner=owner, **defaults)


class NotificationBase(TestCase):
    def setUp(self):
        self.client = Client(schema)

    def ctx(self, user):
        r = RequestFactory().post("/graphql/")
        r.user = user
        return r

    def future(self, **kw):
        return (timezone.now() + timedelta(**kw)).isoformat()


class NotificationApiTests(NotificationBase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        notify(recipient=self.alice, notification_type=Notification.TYPE_SYSTEM,
               title="A1", body="one")
        notify(recipient=self.alice, notification_type=Notification.TYPE_BOOKING,
               title="A2", body="two")
        notify(recipient=self.bob, notification_type=Notification.TYPE_SYSTEM,
               title="B1", body="bob's")

    def test_page_returns_only_my_notifications(self):
        res = self.client.execute(MY_NOTIFS, context=self.ctx(self.alice))
        self.assertIsNone(res.get("errors"))
        page = res["data"]["myNotificationsPage"]
        self.assertEqual(page["totalCount"], 2)
        titles = {i["title"] for i in page["items"]}
        self.assertEqual(titles, {"A1", "A2"})

    def test_newest_first(self):
        res = self.client.execute(MY_NOTIFS, context=self.ctx(self.alice))
        items = res["data"]["myNotificationsPage"]["items"]
        self.assertEqual(items[0]["title"], "A2")  # created last

    def test_unread_count(self):
        res = self.client.execute(UNREAD, context=self.ctx(self.alice))
        self.assertEqual(res["data"]["myUnreadNotificationCount"], 2)

    def test_unread_only_filter(self):
        first = Notification.objects.filter(recipient=self.alice).first()
        first.is_read = True
        first.save(update_fields=["is_read"])
        res = self.client.execute(
            MY_NOTIFS, variables={"unread": True}, context=self.ctx(self.alice)
        )
        self.assertEqual(res["data"]["myNotificationsPage"]["totalCount"], 1)

    def test_mark_one_read(self):
        nid = Notification.objects.filter(recipient=self.alice).first().id
        res = self.client.execute(
            MARK, variables={"id": str(nid)}, context=self.ctx(self.alice)
        )
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["markNotificationRead"]["notification"]["isRead"])
        self.assertEqual(
            self.client.execute(UNREAD, context=self.ctx(self.alice))["data"][
                "myUnreadNotificationCount"], 1)

    def test_cannot_mark_another_users_notification(self):
        bob_nid = Notification.objects.filter(recipient=self.bob).first().id
        res = self.client.execute(
            MARK, variables={"id": str(bob_nid)}, context=self.ctx(self.alice)
        )
        self.assertIsNotNone(res.get("errors"))  # not found for alice
        self.assertFalse(Notification.objects.get(id=bob_nid).is_read)

    def test_mark_all_read(self):
        res = self.client.execute(MARK_ALL, context=self.ctx(self.alice))
        self.assertEqual(res["data"]["markAllNotificationsRead"]["updated"], 2)
        self.assertEqual(
            self.client.execute(UNREAD, context=self.ctx(self.alice))["data"][
                "myUnreadNotificationCount"], 0)
        # Bob's notification is untouched.
        self.assertEqual(
            Notification.objects.filter(recipient=self.bob, is_read=False).count(), 1)

    def test_requires_authentication(self):
        res = self.client.execute(MY_NOTIFS, context=self.ctx(AnonymousUser()))
        self.assertIsNotNone(res.get("errors"))


class NotificationTriggerTests(NotificationBase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("owner", role="station_owner")
        self.driver = make_user("driver")
        self.station = make_station(self.owner)

    def _create_booking(self):
        res = self.client.execute(
            CREATE_BOOKING,
            variables={"s": str(self.station.id),
                       "a": self.future(days=1),
                       "b": self.future(days=1, hours=1)},
            context=self.ctx(self.driver),
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        return res["data"]["createBooking"]["booking"]["id"]

    def test_booking_request_notifies_owner(self):
        self._create_booking()
        n = Notification.objects.filter(
            recipient=self.owner, notification_type=Notification.TYPE_BOOKING
        )
        self.assertEqual(n.count(), 1)
        self.assertIn("driver", n.first().body)

    def test_booking_approval_notifies_customer(self):
        booking_id = self._create_booking()
        res = self.client.execute(
            UPDATE_BOOKING, variables={"id": booking_id, "s": "approved"},
            context=self.ctx(self.owner),
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        n = Notification.objects.filter(
            recipient=self.driver, notification_type=Notification.TYPE_BOOKING)
        self.assertEqual(n.count(), 1)
        self.assertIn("approved", n.first().title)

    def test_owner_booking_own_station_no_self_notification(self):
        # An owner booking their own station must not notify themselves.
        with override_settings(BOOKING_ALLOW_OWNER_SELF_BOOKING=True):
            res = self.client.execute(
                CREATE_BOOKING,
                variables={"s": str(self.station.id), "a": self.future(days=2),
                           "b": self.future(days=2, hours=1)},
                context=self.ctx(self.owner),
            )
            self.assertIsNone(res.get("errors"), res.get("errors"))
        self.assertEqual(Notification.objects.filter(recipient=self.owner).count(), 0)

    @override_settings(REVIEW_REQUIRE_COMPLETED_BOOKING=False)
    def test_review_notifies_owner(self):
        res = self.client.execute(
            CREATE_REVIEW,
            variables={"s": str(self.station.id), "r": 5, "c": "Great"},
            context=self.ctx(self.driver),
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        n = Notification.objects.filter(
            recipient=self.owner, notification_type=Notification.TYPE_REVIEW)
        self.assertEqual(n.count(), 1)

    def test_owner_approval_notifies_owner(self):
        pending = make_user("pending_owner", role="station_owner",
                            owner_status=User.OWNER_PENDING)
        admin = make_user("admin1", role="admin")
        approve_owner(actor=admin, target=pending)
        n = Notification.objects.filter(recipient=pending)
        self.assertEqual(n.count(), 1)
        self.assertIn("approved", n.first().title.lower())

    def test_owner_rejection_notifies_owner_with_reason(self):
        pending = make_user("pending2", role="station_owner",
                            owner_status=User.OWNER_PENDING)
        admin = make_user("admin2", role="admin")
        reject_owner(actor=admin, target=pending, reason="Licence unverified")
        n = Notification.objects.filter(recipient=pending).first()
        self.assertIsNotNone(n)
        self.assertIn("Licence unverified", n.body)

    def test_owner_signup_notifies_all_admins(self):
        admin_a = make_user("admin_a", role="admin")
        admin_b = make_user("admin_b", role="admin")
        make_user("inactive_admin", role="admin", is_active=False)

        create_account("fresh_owner", "fresh@x.com", "483920", is_station_owner=True)

        self.assertEqual(Notification.objects.filter(recipient=admin_a).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=admin_b).count(), 1)
        # Inactive admins are skipped.
        self.assertEqual(
            Notification.objects.filter(recipient__username="inactive_admin").count(), 0)

    def test_customer_signup_notifies_no_admin(self):
        make_user("admin_c", role="admin")
        create_account("plain_customer", "plain@x.com", "483920", is_station_owner=False)
        self.assertEqual(Notification.objects.count(), 0)


class NotificationPreferenceTests(NotificationBase):
    def setUp(self):
        super().setUp()
        self.user = make_user("prefuser")

    def test_defaults_all_on_without_a_row(self):
        res = self.client.execute(PREFS, context=self.ctx(self.user))
        self.assertIsNone(res.get("errors"))
        prefs = res["data"]["myNotificationPreferences"]
        self.assertEqual(
            prefs,
            {"enabled": True, "booking": True, "review": True,
             "station": True, "system": True},
        )
        # Reading defaults must not create a row.
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())

    def test_partial_update_persists_and_leaves_others_default(self):
        res = self.client.execute(
            UPDATE_PREFS, variables={"booking": False}, context=self.ctx(self.user)
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        prefs = res["data"]["updateNotificationPreferences"]["preferences"]
        self.assertFalse(prefs["booking"])
        self.assertTrue(prefs["review"])
        self.assertTrue(prefs["enabled"])
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(pref.booking)
        self.assertTrue(pref.review)

    def test_category_off_suppresses_only_that_category(self):
        NotificationPreference.objects.create(user=self.user, booking=False)
        booking = notify(
            recipient=self.user, notification_type=Notification.TYPE_BOOKING,
            title="B",
        )
        review = notify(
            recipient=self.user, notification_type=Notification.TYPE_REVIEW,
            title="R",
        )
        self.assertIsNone(booking)  # suppressed
        self.assertIsNotNone(review)  # allowed
        self.assertEqual(
            Notification.objects.filter(recipient=self.user).count(), 1)

    def test_master_off_suppresses_every_category(self):
        NotificationPreference.objects.create(user=self.user, enabled=False)
        for ntype in (Notification.TYPE_BOOKING, Notification.TYPE_SYSTEM):
            self.assertIsNone(
                notify(recipient=self.user, notification_type=ntype, title="x"))
        self.assertEqual(
            Notification.objects.filter(recipient=self.user).count(), 0)

    def test_notify_admins_skips_admins_who_opted_out(self):
        opted_in = make_user("admin_in", role="admin")
        opted_out = make_user("admin_out", role="admin")
        NotificationPreference.objects.create(user=opted_out, station=False)
        notify_admins(
            notification_type=Notification.TYPE_STATION, title="New owner",
        )
        self.assertEqual(
            Notification.objects.filter(recipient=opted_in).count(), 1)
        self.assertEqual(
            Notification.objects.filter(recipient=opted_out).count(), 0)

    def test_notify_admins_can_exclude_the_actor(self):
        actor = make_user("admin_actor", role="admin")
        other = make_user("admin_other", role="admin")
        notify_admins(
            notification_type=Notification.TYPE_STATION, title="New owner",
            exclude_id=actor.id,
        )
        self.assertEqual(Notification.objects.filter(recipient=actor).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=other).count(), 1)

    def test_requires_authentication(self):
        res = self.client.execute(PREFS, context=self.ctx(AnonymousUser()))
        self.assertIsNotNone(res.get("errors"))
