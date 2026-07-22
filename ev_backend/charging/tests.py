"""Charging sessions: QR start, live metering, dual/auto stop, booking->done."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from graphene.test import Client

from bookings.models import Booking
from charging import service
from charging.models import ChargingSession
from ev_backend.schema import schema
from stations.models import Station

User = get_user_model()

START = """
mutation($code:String!,$booking:ID){
  startChargingSession(chargerCode:$code, bookingId:$booking){
    session { sessionId stationId chargerCode status energyDeliveredKwh cost pricePerKwh bookingId }
  }
}
"""
STOP = """
mutation($id:ID!){
  stopChargingSession(sessionId:$id){
    session { sessionId status endedAt energyDeliveredKwh cost endReason }
  }
}
"""
SESSION = """
query($id:ID!){
  chargingSession(sessionId:$id){
    sessionId status energyDeliveredKwh cost batteryPercent endReason endedAt
  }
}
"""
ACTIVE = """
query{ myActiveChargingSession { sessionId status } }
"""


def make_user(username, is_active=True):
    return User.objects.create_user(
        username=username, email=f"{username}@x.com", password="123456",
        is_active=is_active,
    )


class ChargingBase(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.owner = make_user("owner")
        self.driver = make_user("driver")
        self.station = Station.objects.create(
            owner=self.owner, name="S", location="L", latitude=1.0, longitude=1.0,
            availability="24/7", charger_type="ccs", charge_mode="dc",
            num_of_charger=1, power_output_kw=50.0, price_per_kwh=10,
        )

    def ctx(self, user):
        r = RequestFactory().post("/graphql/")
        r.user = user
        return r

    def approved_booking(self, user=None):
        now = timezone.now()
        return Booking.objects.create(
            user=user or self.driver, station=self.station, status='approved',
            start_time=now, end_time=now + timedelta(hours=1),
        )

    def start(self, user=None, booking=None):
        res = self.client.execute(
            START,
            variables={"code": self.station.charger_code,
                       "booking": str(booking.id) if booking else None},
            context=self.ctx(user or self.driver),
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        return res["data"]["startChargingSession"]["session"]


class StartTests(ChargingBase):
    def test_start_requires_an_approved_booking_by_default(self):
        res = self.client.execute(
            START, variables={"code": self.station.charger_code, "booking": None},
            context=self.ctx(self.driver),
        )
        self.assertIsNotNone(res.get("errors"))  # no booking, walk-up off

    def test_scan_starts_an_active_session(self):
        self.approved_booking()
        s = self.start()
        self.assertEqual(s["status"], "active")
        self.assertEqual(float(s["energyDeliveredKwh"]), 0.0)
        self.assertEqual(float(s["pricePerKwh"]), 10.0)
        self.assertTrue(s["sessionId"])

    def test_unknown_charger_is_rejected(self):
        res = self.client.execute(
            START, variables={"code": "nope", "booking": None},
            context=self.ctx(self.driver),
        )
        self.assertIsNotNone(res.get("errors"))

    def test_charger_in_use_is_rejected(self):
        self.approved_booking()
        self.start()
        other = make_user("other")
        Booking.objects.create(
            user=other, station=self.station, status='approved',
            start_time=timezone.now(), end_time=timezone.now() + timedelta(hours=1),
        )
        res = self.client.execute(
            START, variables={"code": self.station.charger_code, "booking": None},
            context=self.ctx(other),
        )
        self.assertIsNotNone(res.get("errors"))  # occupied

    def test_one_active_session_per_user(self):
        self.approved_booking()
        self.start()
        # A second charger, but the user already has an active session.
        s2 = Station.objects.create(
            owner=self.owner, name="S2", location="L", latitude=1.0, longitude=1.0,
            availability="24/7", charger_type="ccs2", charge_mode="dc",
            num_of_charger=1, power_output_kw=50.0, price_per_kwh=10,
        )
        Booking.objects.create(
            user=self.driver, station=s2, status='approved',
            start_time=timezone.now(), end_time=timezone.now() + timedelta(hours=1),
        )
        res = self.client.execute(
            START, variables={"code": s2.charger_code, "booking": None},
            context=self.ctx(self.driver),
        )
        self.assertIsNotNone(res.get("errors"))

    @override_settings(CHARGING_ALLOW_WALKUP=True)
    def test_walkup_allowed_when_enabled(self):
        s = self.start()  # no booking
        self.assertEqual(s["status"], "active")
        self.assertIsNone(s["bookingId"])

    def test_requires_authentication(self):
        res = self.client.execute(
            START, variables={"code": self.station.charger_code, "booking": None},
            context=self.ctx(AnonymousUser()),
        )
        self.assertIsNotNone(res.get("errors"))


class MeteringTests(ChargingBase):
    def _backdate(self, session_id, seconds):
        """Pretend the session started `seconds` ago so the simulator meters it."""
        ChargingSession.objects.filter(id=session_id).update(
            started_at=timezone.now() - timedelta(seconds=seconds))

    @override_settings(CHARGING_SIM_FULL_SECONDS=3600)
    def test_polling_shows_energy_and_cost_rising(self):
        self.approved_booking()
        s = self.start()
        self._backdate(s["sessionId"], 360)  # 6 min at 50kW -> 5 kWh
        res = self.client.execute(
            SESSION, variables={"id": s["sessionId"]}, context=self.ctx(self.driver))
        data = res["data"]["chargingSession"]
        self.assertAlmostEqual(float(data["energyDeliveredKwh"]), 5.0, places=1)
        self.assertAlmostEqual(float(data["cost"]), 50.0, places=1)  # 5 kWh * 10
        self.assertEqual(data["status"], "active")

    @override_settings(CHARGING_SIM_FULL_SECONDS=3600)
    def test_stop_finalizes_with_cost_and_marks_booking_done(self):
        booking = self.approved_booking()
        s = self.start(booking=booking)
        self._backdate(s["sessionId"], 720)  # 12 min at 50kW -> 10 kWh
        res = self.client.execute(
            STOP, variables={"id": s["sessionId"]}, context=self.ctx(self.driver))
        data = res["data"]["stopChargingSession"]["session"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["endReason"], "stopped_by_user")
        self.assertIsNotNone(data["endedAt"])
        self.assertAlmostEqual(float(data["cost"]), 100.0, places=0)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "done")

    @override_settings(CHARGING_SIM_FULL_SECONDS=600)
    def test_full_auto_completes_on_read_without_a_client_stop(self):
        self.approved_booking()
        s = self.start()
        self._backdate(s["sessionId"], 700)  # past full (600s)
        res = self.client.execute(
            SESSION, variables={"id": s["sessionId"]}, context=self.ctx(self.driver))
        data = res["data"]["chargingSession"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["endReason"], "full")
        self.assertIsNotNone(data["endedAt"])

    @override_settings(CHARGING_SIM_FULL_SECONDS=3600)
    def test_interruption_ends_with_partial_cost(self):
        booking = self.approved_booking()
        s = self.start(booking=booking)
        self._backdate(s["sessionId"], 360)  # 5 kWh so far
        session = ChargingSession.objects.get(id=s["sessionId"])
        service.report_interruption(session)
        session.refresh_from_db()
        self.assertEqual(session.status, "interrupted")
        self.assertEqual(session.end_reason, "interrupted")
        self.assertAlmostEqual(session.cost, 50.0, places=0)  # partial
        booking.refresh_from_db()
        self.assertEqual(booking.status, "done")

    def test_stop_is_idempotent(self):
        self.approved_booking()
        s = self.start()
        self.client.execute(STOP, variables={"id": s["sessionId"]},
                            context=self.ctx(self.driver))
        res = self.client.execute(STOP, variables={"id": s["sessionId"]},
                                  context=self.ctx(self.driver))
        self.assertIsNone(res.get("errors"), res.get("errors"))
        self.assertEqual(
            res["data"]["stopChargingSession"]["session"]["status"], "completed")

    def test_cannot_touch_another_users_session(self):
        self.approved_booking()
        s = self.start()
        intruder = make_user("intruder")
        res = self.client.execute(
            SESSION, variables={"id": s["sessionId"]}, context=self.ctx(intruder))
        self.assertIsNotNone(res.get("errors"))


class ActiveSessionTests(ChargingBase):
    def test_my_active_session_resumes(self):
        self.approved_booking()
        s = self.start()
        res = self.client.execute(ACTIVE, context=self.ctx(self.driver))
        self.assertEqual(
            res["data"]["myActiveChargingSession"]["sessionId"], s["sessionId"])

    def test_no_active_session_returns_null(self):
        res = self.client.execute(ACTIVE, context=self.ctx(self.driver))
        self.assertIsNone(res["data"]["myActiveChargingSession"])
