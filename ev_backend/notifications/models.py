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
