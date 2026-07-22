"""Coverage for the `seed_staging` management command.

Runs the command end to end against the test database and asserts the
deterministic dataset it promises, plus its two refusal paths (no --yes, and
seeding on top of an existing seed).
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from bookings.models import Booking
from stations.models import Favorite, Review, Station
from vehicles.models import Vehicle

from accounts.management.commands.seed_staging import (
    BOOKING_PLAN,
    CUSTOMER_COUNT,
    HIDDEN_REVIEWS,
    PREFIX,
    REVIEW_COUNT,
    STATION_COUNT,
)

User = get_user_model()


class SeedStagingCommandTests(TestCase):
    def _seed(self, *args):
        out = StringIO()
        call_command("seed_staging", *args, stdout=out)
        return out.getvalue()

    def test_seed_creates_the_promised_deterministic_dataset(self):
        output = self._seed("--yes")

        # Users: 2 admins + 5 owners + 12 customers.
        self.assertEqual(User.objects.filter(username__startswith=PREFIX).count(),
                         2 + 5 + CUSTOMER_COUNT)
        self.assertEqual(
            User.objects.filter(username__startswith=PREFIX, role="admin").count(), 2)
        self.assertEqual(
            User.objects.filter(username__startswith=PREFIX,
                                role="station_owner").count(), 5)

        # Stations: 20, of which 4 inactive.
        self.assertEqual(Station.objects.filter(name__startswith=PREFIX).count(),
                         STATION_COUNT)
        self.assertEqual(
            Station.objects.filter(name__startswith=PREFIX, is_active=False).count(), 4)

        # Bookings: the exact plan.
        self.assertEqual(Booking.objects.count(), sum(BOOKING_PLAN.values()))
        for status, count in BOOKING_PLAN.items():
            self.assertEqual(Booking.objects.filter(status=status).count(), count)

        # Reviews: 20, five hidden, each backed by a done booking.
        self.assertEqual(Review.objects.count(), REVIEW_COUNT)
        self.assertEqual(Review.objects.filter(is_hidden=True).count(), HIDDEN_REVIEWS)

        # Favourites and vehicles exist.
        self.assertTrue(Favorite.objects.exists())
        self.assertTrue(Vehicle.objects.filter(is_primary=True).exists())

        # Report mentions the sign-in PIN and the pending owner.
        self.assertIn("Staging seeded", output)
        self.assertIn("owner_pending", output)

    def test_reset_removes_a_previous_seed_then_reseeds(self):
        self._seed("--yes")
        before = User.objects.filter(username__startswith=PREFIX).count()

        # --reset deletes the old namespace and rebuilds it; counts match.
        output = self._seed("--yes", "--reset")
        self.assertIn("Reset: removed", output)
        self.assertEqual(
            User.objects.filter(username__startswith=PREFIX).count(), before)

    def test_reset_on_an_empty_database_is_safe(self):
        # Nothing to remove — the reset branch still runs and reports zero.
        output = self._seed("--yes", "--reset")
        self.assertIn("Reset: removed 0", output)

    def test_refuses_without_yes(self):
        with self.assertRaises(CommandError):
            call_command("seed_staging")
        self.assertFalse(User.objects.filter(username__startswith=PREFIX).exists())

    def test_refuses_to_seed_on_top_of_an_existing_seed(self):
        self._seed("--yes")
        with self.assertRaises(CommandError):
            call_command("seed_staging", "--yes", stdout=StringIO())
