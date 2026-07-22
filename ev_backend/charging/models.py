from django.conf import settings
from django.db import models

from stations.models import Station


class ChargingSession(models.Model):
    """One real, metered charging session (W9).

    Started when a customer scans a charger's QR and taps Start; ended by the
    customer, by the charger reporting full, or by an interruption. ``cost`` is
    ALWAYS metered energy × the price snapshotted at start — never time-based —
    so a slow charger never overcharges and a price change mid-session cannot
    move a running cost.

    Two partial-unique constraints keep the physical world honest: a user can
    have only one session running, and a charger can host only one. Both are
    database guarantees, not application hopes a double-scan could race past.
    """

    STATUS_ACTIVE = 'active'
    STATUS_COMPLETED = 'completed'
    STATUS_INTERRUPTED = 'interrupted'
    STATUS_CHOICES = (
        (STATUS_ACTIVE, 'Active'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_INTERRUPTED, 'Interrupted'),
    )

    # end_reason values (free string, mirroring the booking-status convention).
    REASON_FULL = 'full'
    REASON_STOPPED = 'stopped_by_user'
    REASON_INTERRUPTED = 'interrupted'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='charging_sessions',
    )
    station = models.ForeignKey(
        Station, on_delete=models.CASCADE, related_name='charging_sessions',
    )
    # SET_NULL, not CASCADE: a session is a metered fact that must outlive the
    # booking it was authorised by, the same reasoning AuditLog uses.
    booking = models.ForeignKey(
        'bookings.Booking', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='charging_sessions',
    )
    charger_code = models.CharField(max_length=32)
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTIVE,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True, default=None)
    energy_delivered_kwh = models.FloatField(default=0.0)
    # Snapshot at start so the running cost is stable against station edits.
    price_per_kwh = models.FloatField()
    battery_percent = models.FloatField(null=True, blank=True, default=None)
    end_reason = models.CharField(max_length=20, blank=True, default='')

    class Meta:
        ordering = ('-started_at', '-id')
        constraints = [
            models.UniqueConstraint(
                fields=['user'], condition=models.Q(status='active'),
                name='one_active_session_per_user',
            ),
            models.UniqueConstraint(
                fields=['charger_code'], condition=models.Q(status='active'),
                name='one_active_session_per_charger',
            ),
            models.CheckConstraint(
                check=models.Q(energy_delivered_kwh__gte=0),
                name='session_energy_non_negative',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'status'], name='session_user_status_idx'),
        ]

    @property
    def cost(self):
        """Authoritative cost: metered energy × the price snapshotted at start."""
        return round(self.energy_delivered_kwh * self.price_per_kwh, 2)

    def __str__(self):
        return f"Session {self.pk} @ {self.charger_code} ({self.status})"
