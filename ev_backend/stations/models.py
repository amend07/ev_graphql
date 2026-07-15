from django.db import models
from django.db.models.functions import Coalesce
from django.conf import settings


def review_stats():
    """Rating annotations for a Station queryset, over VISIBLE reviews only.

    One definition, used by every path that puts a rating in front of anyone: the
    public list, the cached list, an owner's own stations, the admin console.
    Moderation is only real if it reaches all of them, and the way that breaks is
    a new resolver hand-rolling ``Count("reviews")`` and quietly counting the
    hidden rows back in. Import this instead of writing that.

    Lives here rather than in ``schema.py`` because it is a queryset concern and
    because ``cache.py`` needs it too — and ``schema`` already imports ``cache``,
    so the reverse would be a cycle.
    """
    return dict(
        num_of_rate=models.Count("reviews", filter=Review.VISIBLE_FROM_STATION),
        average_rate=Coalesce(
            models.Avg("reviews__rating", filter=Review.VISIBLE_FROM_STATION), 0.0,
        ),
    )


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

    # Moderation (Sprint B2.1).
    #
    # Hiding is reversible; deleting is not. A hidden review must be invisible in
    # EVERY public read AND excluded from every rating aggregate — a moderated
    # review that still moves the station's average has not been moderated, it has
    # only been made harder to read. `visible()` is the single definition of that,
    # so a new read path cannot forget the filter by writing its own query.
    #
    # Hiding deliberately does NOT free the one-review-per-user constraint: an
    # author must not be able to evade moderation by posting the same content
    # again. They can still edit or delete their own review, which is theirs to do.
    is_hidden = models.BooleanField(
        default=False,
        help_text="Hidden by a moderator: invisible to the public and excluded from ratings.",
    )

    class Meta:
        indexes = [
            # Every public read filters on this pair.
            models.Index(fields=['station', 'is_hidden'], name='review_station_hidden_idx'),
        ]
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

    # Aggregation-side twin of `visible()`, for Count/Avg over a Station's
    # `reviews` related name. Kept beside the field so the cached public list and
    # the live resolvers cannot drift apart on what "counts".
    VISIBLE_FROM_STATION = models.Q(reviews__is_hidden=False)

    @classmethod
    def visible(cls):
        """Reviews the public may see. The single definition — use it everywhere."""
        return cls.objects.filter(is_hidden=False)

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
