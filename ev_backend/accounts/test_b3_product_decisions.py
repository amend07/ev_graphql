"""Characterization tests for undecided behaviour (Sprint B3, Priority 3).

These tests do NOT assert what the product should do. They pin what it does
today, so that changing it is a deliberate act with a failing test attached
rather than a silent side effect of some unrelated refactor.

Read this before "fixing" a failure here: if one of these breaks, you have either
made a product decision or made a mistake, and the test cannot tell which. Only a
human can. Update the test in the same commit as the decision, and say who made
it in the message.

Everything here is listed in the known-deviations table of
BACKEND_ENGINEERING_PRINCIPLES.md.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from bookings.models import Booking
from stations.models import Station

from . import administration

User = get_user_model()


def make_owner(username='decowner', status=None):
    return User.objects.create_user(
        username=username, email=f'{username}@x.com', password='123456',
        role='station_owner', owner_status=status or User.OWNER_APPROVED,
    )


def make_station(owner, name='Station'):
    return Station.objects.create(
        owner=owner, name=name, location='Loc', latitude=1.0, longitude=1.0,
        availability='24/7', charger_type='CCS', num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )


class RevokedOwnerLeavesStationsOnline(TestCase):
    """OPEN PRODUCT DECISION — do not resolve this in code.

    Rejecting an approved station owner withdraws their ability to MANAGE
    stations. It does nothing to the stations themselves: they stay active,
    publicly listed, and bookable by customers, and nobody can now edit them —
    the owner is refused by `station_owner_required`, and an admin has no
    per-station edit mutation at all.

    Whether that is right depends on why the platform rejects owners, which is a
    question for the business and not for this file:

      Option A — revoking an owner takes their stations offline.
        Right if rejection means "this operator is not fit to run chargers":
        leaving their sites bookable would send customers to them.
        Cost: it silently cancels nothing — the bookings already made on those
        stations are NOT touched by deactivation, so customers keep reservations
        at a station that has vanished from search. That needs its own answer.

      Option B — stations stay online (today's behaviour).
        Right if rejection is administrative — an unverified document, a duplicate
        account — and the hardware is fine.
        Cost: the orphan state above. An admin can take each station down by hand
        with `deactivateStation`, which is a lever, not a policy.

    THE DECISION GOES IN exactly one place: `accounts.administration.reject_owner`,
    after the `owner_status` write. The mechanism for Option A already exists and
    is audited — `stations.administration.set_station_active(...)` — so the change
    is a loop over `Station.objects.filter(owner=target, is_active=True)`, not an
    architecture. Import it inside the function, as `deletion_summary` already
    imports station models, to keep accounts→stations lazy.

    Implementing Option A means changing THIS TEST FIRST.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username='decadmin', email='decadmin@x.com', password='123456', role='admin',
        )
        self.owner = make_owner()
        self.station = make_station(self.owner)
        self.customer = User.objects.create_user(
            username='deccust', email='deccust@x.com', password='123456',
        )
        self.request = RequestFactory().post('/graphql/')
        self.request.user = self.admin

    def test_rejecting_an_owner_leaves_their_stations_active(self):
        administration.reject_owner(
            actor=self.admin, target=self.owner, reason='Unverified documents',
            request=self.request,
        )

        self.station.refresh_from_db()
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.owner_status, User.OWNER_REJECTED)
        self.assertTrue(
            self.station.is_active,
            "Stations of a rejected owner are still active. If this now fails, "
            "someone chose Option A — see this class's docstring and update it.",
        )

    def test_a_rejected_owners_station_is_still_publicly_listed_and_bookable(self):
        # The consequence that matters: not an internal flag, but a customer
        # being sent to a charger whose operator was just turned away.
        administration.reject_owner(
            actor=self.admin, target=self.owner, reason='Unverified documents',
            request=self.request,
        )

        self.assertIn(
            self.station, Station.objects.filter(is_active=True),
            "A rejected owner's station is still in the public listing today.",
        )
        booking = Booking.objects.create(
            user=self.customer, station=self.station, status='pending',
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=1),
        )
        self.assertEqual(booking.station.owner.owner_status, User.OWNER_REJECTED)

    def test_the_rejected_owner_cannot_manage_the_station_they_still_own(self):
        # The asymmetry that makes this a real question: the station is live, and
        # now NOBODY can edit it. The owner is refused, and there is no admin
        # station-edit mutation — only activate/deactivate.
        administration.reject_owner(
            actor=self.admin, target=self.owner, reason='Unverified documents',
            request=self.request,
        )
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_approved_owner())
        self.assertTrue(self.owner.is_active, "Rejection is not a ban.")

    def test_the_mechanism_for_option_a_already_exists_and_is_audited(self):
        # Guards the scaffolding rather than the policy: whichever way the
        # decision goes, the lever Option A needs must stay wired and audited.
        from stations import administration as station_admin

        self.assertTrue(callable(station_admin.set_station_active))

        station_admin.set_station_active(
            actor=self.admin, station=self.station, is_active=False,
            reason='manual takedown', request=self.request,
        )
        self.station.refresh_from_db()
        self.assertFalse(self.station.is_active)

        from .models import AuditLog

        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.ACTION_STATION_DEACTIVATED,
                target_id=str(self.station.pk),
            ).exists(),
            "Option A must not be implementable without an audit trail.",
        )
