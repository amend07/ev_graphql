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
    is_favorite = models.BooleanField(default=False)

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

    # Reviews
    num_of_rate = models.PositiveIntegerField(default=0)
    average_rate = models.FloatField(default=0.0)
    review = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.location}"