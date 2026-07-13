from django.db import models
from django.conf import settings
from stations.models import Station

class Booking(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
        ('done', 'Done'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookings')
    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='bookings')
    
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    cancel_reason = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # Allowed status transitions (single source of truth for state changes).
    VALID_TRANSITIONS = {
        'pending': {'approved', 'rejected', 'cancelled'},
        'approved': {'cancelled', 'done'},
        'rejected': set(),
        'cancelled': set(),
        'done': set(),
    }

    # Statuses that still occupy a charger slot for overlap purposes.
    ACTIVE_STATUSES = ('pending', 'approved')

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_time__gt=models.F('start_time')),
                name='booking_end_after_start',
            ),
        ]
        indexes = [
            models.Index(fields=['station', 'status'], name='booking_station_status_idx'),
            models.Index(fields=['station', 'start_time', 'end_time'], name='booking_station_time_idx'),
            models.Index(fields=['user', 'status'], name='booking_user_status_idx'),
        ]

    def can_transition_to(self, new_status):
        return new_status in self.VALID_TRANSITIONS.get(self.status, set())

    def __str__(self):
        return f"{self.user.username} - {self.station.name} ({self.status})"
