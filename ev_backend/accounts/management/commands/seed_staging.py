"""Seed a staging database with a realistic, deterministic dataset.

    manage.py seed_staging --yes
    manage.py seed_staging --yes --reset     # rebuild from scratch

Why this exists: without data, an integration pass against staging tests nothing.
Every screen renders its empty state, every filter matches everything, every
pagination control is disabled, and the whole exercise reports success while
exercising none of the code that matters.

SAFETY — how this cannot damage real data:

* **Namespaced.** Every row it creates is prefixed `seed_`. It never reads,
  updates or deletes anything else, so pointing it at a database with real
  accounts adds rows but destroys nothing.
* **`--reset` deletes only what this command made** — the `seed_` namespace,
  never a table.
* **`--yes` is required.** Seeding is a write to whatever database the
  environment points at, and that is worth one deliberate keystroke.

DETERMINISTIC: fixed IDs and offsets, no randomness. Re-running produces the same
platform, so a bug found on staging can be reproduced exactly, and a test can
assert against known values rather than "some station".

The distribution is not uniform, on purpose — see `_seed_bookings`.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bookings.models import Booking
from stations.models import Favorite, Review, Station

User = get_user_model()

PREFIX = 'seed_'
# Staging only, and printed on completion so nobody has to guess. The backend's
# validator requires exactly 6 digits.
SEED_PIN = '123456'

CUSTOMER_COUNT = 12
STATION_COUNT = 20
INACTIVE_STATIONS = 4
REVIEW_COUNT = 20
HIDDEN_REVIEWS = 5

# 50 bookings. Deliberately NOT 10 per status: `done` is over-weighted so that
# every seeded review sits on a genuinely completed session at that station,
# which is what the backend's own rule requires
# (REVIEW_REQUIRE_COMPLETED_BOOKING). Seeding 20 reviews on top of 10 completed
# bookings would produce a platform the application itself could never create —
# and W6 would then be testing a state that cannot occur.
BOOKING_PLAN = {
    'done': 20,
    'approved': 10,
    'pending': 8,
    'cancelled': 7,
    'rejected': 5,
}

CHARGER_TYPES = ['CCS2', 'CHAdeMO', 'Type 2', 'CCS2', 'Type 2']
LOCATIONS = [
    'Bole Road, Addis Ababa',
    'Kazanchis, Addis Ababa',
    'Piassa, Addis Ababa',
    'CMC, Addis Ababa',
    'Megenagna, Addis Ababa',
]


class Command(BaseCommand):
    help = "Seed staging with a deterministic, realistic dataset (namespaced `seed_`)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes', action='store_true',
            help="Required. Confirms you mean to write to this database.",
        )
        parser.add_argument(
            '--reset', action='store_true',
            help="Delete previously seeded rows first. Only touches the `seed_` namespace.",
        )

    def handle(self, *args, **options):
        if not options['yes']:
            raise CommandError(
                "Refusing to write without --yes. This seeds whatever database "
                "the current environment points at."
            )

        if options['reset']:
            self._reset()

        if User.objects.filter(username__startswith=PREFIX).exists():
            raise CommandError(
                "Seed data already exists. Re-run with --reset to rebuild it, or "
                "leave it alone — this command does not merge into existing seeds."
            )

        with transaction.atomic():
            admins = self._seed_admins()
            owners = self._seed_owners(reviewer=admins[0])
            customers = self._seed_customers()
            stations = self._seed_stations(owners)
            bookings = self._seed_bookings(customers, stations)
            reviews = self._seed_reviews(bookings)
            favorites = self._seed_favorites(customers, stations)

        self._report(admins, owners, customers, stations, bookings, reviews, favorites)

    # ── seeding ──────────────────────────────────────────────────────────

    def _user(self, username, *, role='user', **kw):
        return User.objects.create_user(
            username=f'{PREFIX}{username}',
            email=f'{PREFIX}{username}@example.com',
            password=SEED_PIN,
            role=role,
            **kw,
        )

    def _seed_admins(self):
        return [self._user(f'admin_{i}', role='admin', is_staff=True) for i in (1, 2)]

    def _seed_owners(self, *, reviewer):
        """The five owner states an admin has to be able to tell apart.

        These are the cases the console's approval queue and user filters exist
        for, and each has caught a real bug: a pending owner is ACTIVE (W4's
        profile bug reported the opposite), a rejected owner is also active
        (rejection is not a ban), and a deactivated owner is approved but cannot
        sign in — which is a different conversation with support entirely.
        """
        now = timezone.now()
        approved_1 = self._user(
            'owner_approved_1', role='station_owner',
            owner_status=User.OWNER_APPROVED,
        )
        approved_2 = self._user(
            'owner_approved_2', role='station_owner',
            owner_status=User.OWNER_APPROVED,
        )
        pending = self._user(
            'owner_pending', role='station_owner', owner_status=User.OWNER_PENDING,
        )
        rejected = self._user(
            'owner_rejected', role='station_owner', owner_status=User.OWNER_REJECTED,
        )
        deactivated = self._user(
            'owner_deactivated', role='station_owner',
            owner_status=User.OWNER_APPROVED, is_active=False,
        )

        # Provenance for the decided ones (B2.1). A decision with no reviewer is
        # exactly the state B2.1 removed, so seeding one would be seeding a bug.
        for user, decided_days_ago, reason in (
            (approved_1, 30, ''),
            (approved_2, 14, ''),
            (rejected, 7, 'Operating licence could not be verified.'),
            (deactivated, 60, ''),
        ):
            user.reviewer = reviewer
            user.reviewed_at = now - timedelta(days=decided_days_ago)
            user.rejection_reason = reason
            user.save(update_fields=['reviewer', 'reviewed_at', 'rejection_reason'])

        # `pending` keeps reviewer/reviewed_at NULL — nobody has decided yet, and
        # that is the whole point of the queue.
        return {
            'approved_1': approved_1,
            'approved_2': approved_2,
            'pending': pending,
            'rejected': rejected,
            'deactivated': deactivated,
        }

    def _seed_customers(self):
        return [self._user(f'customer_{i:02d}') for i in range(1, CUSTOMER_COUNT + 1)]

    def _seed_stations(self, owners):
        """20 stations, 4 inactive, spread over the owners who can legitimately hold them.

        The pending and rejected owners get none: the backend refuses station
        creation to both, so a station under either could not exist in a real
        platform. Some belong to the deactivated owner — that IS reachable (they
        were approved, built stations, then were deactivated) and it is the case
        the admin station list exists to surface.
        """
        holders = [
            owners['approved_1'],
            owners['approved_2'],
            owners['deactivated'],
        ]
        stations = []
        for i in range(STATION_COUNT):
            owner = holders[i % 3] if i >= 2 else holders[0]
            stations.append(
                Station.objects.create(
                    owner=owner,
                    name=f'{PREFIX}Station {i + 1:02d}',
                    location=LOCATIONS[i % len(LOCATIONS)],
                    # Spread around Addis so a map view has something to show.
                    latitude=9.0 + (i * 0.004),
                    longitude=38.7 + (i * 0.004),
                    availability='24/7' if i % 3 else 'Mon–Fri, 8am–8pm',
                    charger_type=CHARGER_TYPES[i % len(CHARGER_TYPES)],
                    num_of_charger=1 + (i % 4),
                    power_output_kw=[22.0, 50.0, 150.0, 350.0][i % 4],
                    price_per_kwh=round(0.25 + (i % 5) * 0.05, 2),
                    estimated_time_min=[30, 45, 20, 60][i % 4],
                    description=f'Seeded charging station {i + 1}.',
                    contact_info=f'station{i + 1}@example.com',
                    amenities='Wifi, Cafe' if i % 2 else 'Restroom',
                    # The last few are inactive, so the admin console's isActive
                    # filter and the "inactive stations" total have something to
                    # find rather than rendering an empty state.
                    is_active=i < (STATION_COUNT - INACTIVE_STATIONS),
                )
            )
        return stations

    def _seed_bookings(self, customers, stations):
        """50 bookings covering every status.

        Times are coherent with status, because a console that shows a `done`
        booking starting next week is showing a state the app cannot produce:
        completed and cancelled sessions are in the past, pending and approved
        ones in the future.
        """
        now = timezone.now()
        bookings = []
        index = 0
        for status, count in BOOKING_PLAN.items():
            past = status in ('done', 'cancelled', 'rejected')
            for n in range(count):
                customer = customers[index % len(customers)]
                station = stations[index % len(stations)]
                offset = timedelta(days=(n + 1), hours=(index % 6))
                start = (now - offset) if past else (now + offset)
                bookings.append(
                    Booking.objects.create(
                        user=customer,
                        station=station,
                        start_time=start,
                        end_time=start + timedelta(hours=1),
                        status=status,
                        cancel_reason=(
                            'Customer changed plans.' if status == 'cancelled'
                            else 'No chargers free at that time.' if status == 'rejected'
                            else None
                        ),
                    )
                )
                index += 1
        return bookings

    def _seed_reviews(self, bookings):
        """20 reviews, 5 hidden — every one backed by a completed booking.

        Built from the `done` bookings rather than invented, so each review obeys
        the rule the backend enforces on real ones: you may only review a station
        you have finished charging at. `Review` is unique per (user, station), and
        the done bookings are already distinct pairs.
        """
        completed = [b for b in bookings if b.status == 'done'][:REVIEW_COUNT]
        if len(completed) < REVIEW_COUNT:  # pragma: no cover - guarded by BOOKING_PLAN
            raise CommandError(
                f"Only {len(completed)} completed bookings for {REVIEW_COUNT} reviews. "
                f"Raise BOOKING_PLAN['done'] rather than seeding reviews with no session."
            )

        comments = [
            'Fast charger, easy to find.',
            'Worked fine but the cafe was closed.',
            'Great location, will use again.',
            'Cable was damaged, took three tries.',
            'Exactly as described.',
        ]
        reviews = []
        for i, booking in enumerate(completed):
            reviews.append(
                Review.objects.create(
                    user=booking.user,
                    station=booking.station,
                    rating=[5, 4, 3, 5, 1][i % 5],
                    comment=comments[i % len(comments)],
                    # Hidden ones sit at the end so the visible/hidden filter and
                    # the "hidden reviews do not count toward the rating" rule
                    # both have something real to act on.
                    is_hidden=i >= (REVIEW_COUNT - HIDDEN_REVIEWS),
                )
            )
        return reviews

    def _seed_favorites(self, customers, stations):
        favorites = []
        for i, customer in enumerate(customers[:6]):
            for station in stations[i : i + 3]:
                favorites.append(
                    Favorite.objects.create(user=customer, station=station)
                )
        return favorites

    # ── reset / report ───────────────────────────────────────────────────

    def _reset(self):
        """Delete seeded rows only.

        Deleting the users cascades to their stations, bookings, reviews and
        favourites — the same cascade `deleteUser` uses. Seeded stations are
        removed by owner, so nothing outside the namespace is reachable from here.
        """
        seeded = User.objects.filter(username__startswith=PREFIX)
        count = seeded.count()
        Station.objects.filter(name__startswith=PREFIX).delete()
        seeded.delete()
        self.stdout.write(f"Reset: removed {count} seeded accounts and their data.")

    def _report(self, admins, owners, customers, stations, bookings, reviews, favorites):
        active = sum(1 for s in stations if s.is_active)
        hidden = sum(1 for r in reviews if r.is_hidden)
        by_status = {
            status: sum(1 for b in bookings if b.status == status)
            for status in BOOKING_PLAN
        }

        self.stdout.write(self.style.SUCCESS('\nStaging seeded.\n'))
        self.stdout.write(f"  Admins            {len(admins)}")
        self.stdout.write(
            f"  Owners            {len(owners)} "
            f"(2 approved, 1 pending, 1 rejected, 1 deactivated)"
        )
        self.stdout.write(f"  Customers         {len(customers)}")
        self.stdout.write(
            f"  Stations          {len(stations)} ({active} active, "
            f"{len(stations) - active} inactive)"
        )
        self.stdout.write(
            f"  Bookings          {len(bookings)} ("
            + ', '.join(f'{n} {s}' for s, n in by_status.items())
            + ")"
        )
        self.stdout.write(
            f"  Reviews           {len(reviews)} ({hidden} hidden, "
            f"{len(reviews) - hidden} visible)"
        )
        self.stdout.write(f"  Favourites        {len(favorites)}")
        self.stdout.write(
            f"\n  Sign in as any of them with PIN {SEED_PIN}. Admin: "
            f"{PREFIX}admin_1 / owner: {PREFIX}owner_approved_1 / customer: "
            f"{PREFIX}customer_01"
        )
        self.stdout.write(
            f"  Pending approval queue: {PREFIX}owner_pending\n"
        )
