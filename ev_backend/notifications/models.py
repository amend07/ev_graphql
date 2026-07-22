from django.conf import settings
from django.db import models


class Notification(models.Model):
    """A user-facing notification.

    Any user can receive one — customer, station owner, or admin — so the only
    thing that scopes it is the ``recipient`` FK. ``related_id`` is a snapshot of
    the target (a booking or station id) for deep-linking, not an FK: a deleted
    target should never cascade a notification away or error when it is read.
    """

    TYPE_BOOKING = 'booking'
    TYPE_REVIEW = 'review'
    TYPE_STATION = 'station'
    TYPE_SYSTEM = 'system'
    TYPE_CHOICES = [
        (TYPE_BOOKING, 'Booking'),
        (TYPE_REVIEW, 'Review'),
        (TYPE_STATION, 'Station'),
        (TYPE_SYSTEM, 'System'),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    notification_type = models.CharField(
        max_length=16, choices=TYPE_CHOICES, default=TYPE_SYSTEM,
    )
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default='')
    related_id = models.CharField(max_length=64, blank=True, default='')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at', '-id')
        indexes = [
            models.Index(
                fields=['recipient', 'is_read'], name='notif_recipient_read_idx',
            ),
            models.Index(
                fields=['recipient', 'created_at'], name='notif_recipient_created_idx',
            ),
        ]

    def __str__(self):
        state = 'read' if self.is_read else 'unread'
        return f"{self.recipient.username} - {self.notification_type} ({state})"


class NotificationPreference(models.Model):
    """Per-user control over which notifications get created.

    A master ``enabled`` switch plus one flag per :class:`Notification` category.
    Absence of a row means "all on": creation is gated in
    :func:`notifications.service.notify` / ``notify_admins``, and a user who has
    never touched their settings must keep receiving everything. So the DEFAULTS
    here are all True, and callers treat a missing row exactly like an all-True
    row — no backfill needed for existing users.

    The category flags line up 1:1 with ``Notification.TYPE_*`` so gating is a
    single ``getattr(pref, notification_type)``.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_preference',
    )
    # Master switch: off silences every category.
    enabled = models.BooleanField(default=True)
    booking = models.BooleanField(default=True)
    review = models.BooleanField(default=True)
    station = models.BooleanField(default=True)
    system = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def allows(self, notification_type):
        """Whether a notification of this category may be created for this user."""
        if not self.enabled:
            return False
        # Unknown/future categories default to allowed rather than silently dropped.
        return bool(getattr(self, notification_type, True))

    def __str__(self):
        state = 'on' if self.enabled else 'off'
        return f"{self.user.username} notification prefs ({state})"
