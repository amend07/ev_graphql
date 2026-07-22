from django.conf import settings
from django.db import models

from stations.models import Station


class Vehicle(models.Model):
    """A car in a user's garage (Sprint W9).

    Purely personal data: it belongs to exactly one user and is only ever read
    by that user, so the only thing that scopes it is the ``owner`` FK. The
    connector reuses the station's canonical ``CHARGER_TYPE_CHOICES`` so a
    vehicle's plug and a station's socket are the same vocabulary — that is what
    lets "which of my cars fits this station" ever be answerable.

    ``is_primary`` marks the everyday car. At most one per user is primary; that
    is enforced by a partial unique constraint here and kept true by
    ``set_primary`` / the add-and-update mutations, which clear the flag on the
    others in the same transaction.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='vehicles',
    )
    make = models.CharField(max_length=60, help_text="Manufacturer / company.")
    model = models.CharField(max_length=60)
    year = models.PositiveIntegerField()
    battery_capacity_kwh = models.DecimalField(max_digits=6, decimal_places=2)
    charger_type = models.CharField(
        max_length=50, choices=Station.CHARGER_TYPE_CHOICES,
    )
    plate_number = models.CharField(max_length=20)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-is_primary', '-created_at', '-id')
        constraints = [
            # One human can't register the same plate twice.
            models.UniqueConstraint(
                fields=['owner', 'plate_number'],
                name='unique_plate_per_owner',
            ),
            # At most one primary vehicle per owner — a database guarantee, not an
            # application hope a concurrent "set primary" could race past.
            models.UniqueConstraint(
                fields=['owner'],
                condition=models.Q(is_primary=True),
                name='unique_primary_vehicle_per_owner',
            ),
            models.CheckConstraint(
                check=models.Q(battery_capacity_kwh__gt=0),
                name='vehicle_battery_positive',
            ),
        ]
        indexes = [
            models.Index(fields=['owner', '-is_primary'], name='vehicle_owner_primary_idx'),
        ]

    def __str__(self):
        star = '★ ' if self.is_primary else ''
        return f"{star}{self.make} {self.model} ({self.plate_number})"
