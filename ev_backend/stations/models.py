from django.db import models
from django.conf import settings

class Station(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stations"
    )

    # Basic Info
    name = models.CharField(max_length=100)
    contact_info = models.CharField(max_length=100, blank=True, null=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="station_images/", null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Location
    location = models.CharField(max_length=255)
    latitude = models.FloatField()
    longitude = models.FloatField()

    # Availability & Amenities
    availability = models.CharField(max_length=100)
    amenities = models.TextField(blank=True)

    # Charger Info
    charger_type = models.CharField(max_length=50)
    station_count = models.PositiveIntegerField(default=1)
    num_of_charger = models.PositiveIntegerField(default=1)
    power_output_kw = models.FloatField(default=22.0)
    estimated_time_min = models.PositiveIntegerField(default=30)
    price_per_kwh = models.DecimalField(max_digits=6, decimal_places=2)
    charger_brand = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["is_active"], name="station_is_active_idx"),
            models.Index(fields=["owner", "is_active"], name="station_owner_active_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(latitude__gte=-90) & models.Q(latitude__lte=90),
                name="station_latitude_range",
            ),
            models.CheckConstraint(
                check=models.Q(longitude__gte=-180) & models.Q(longitude__lte=180),
                name="station_longitude_range",
            ),
            models.CheckConstraint(
                check=models.Q(price_per_kwh__gte=0),
                name="station_price_non_negative",
            ),
            models.CheckConstraint(
                check=models.Q(power_output_kw__gt=0),
                name="station_power_positive",
            ),
        ]

    def __str__(self):
        return f"{self.name} - {self.location}"


class Review(models.Model):
    station = models.ForeignKey('Station', related_name='reviews', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    rating = models.PositiveIntegerField()  # 1 to 5
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One review per user per station (race-safe at the DB level).
            models.UniqueConstraint(
                fields=["user", "station"], name="unique_review_per_user_station"
            ),
            models.CheckConstraint(
                check=models.Q(rating__gte=1) & models.Q(rating__lte=5),
                name="review_rating_range",
            ),
        ]

    def __str__(self):
        return f"Review by {self.user.username} for {self.station.name} - {self.rating}"


class Favorite(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorites")
    station = models.ForeignKey('stations.Station', on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'station')

    def __str__(self):
        return f"{self.user.username} -> {self.station.name}"
