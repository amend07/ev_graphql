from django.db import migrations, models


class Migration(migrations.Migration):
    """Data-integrity constraints for stations and reviews (Sprint 4):
    coordinate/price/power ranges, one review per user per station, and a
    1–5 rating check. Additive; no columns changed."""

    dependencies = [
        ("stations", "0003_remove_station_is_favorite_favorite"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="station",
            index=models.Index(fields=["is_active"], name="station_is_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="station",
            constraint=models.CheckConstraint(
                check=models.Q(latitude__gte=-90) & models.Q(latitude__lte=90),
                name="station_latitude_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="station",
            constraint=models.CheckConstraint(
                check=models.Q(longitude__gte=-180) & models.Q(longitude__lte=180),
                name="station_longitude_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="station",
            constraint=models.CheckConstraint(
                check=models.Q(price_per_kwh__gte=0),
                name="station_price_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="station",
            constraint=models.CheckConstraint(
                check=models.Q(power_output_kw__gt=0),
                name="station_power_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="review",
            constraint=models.UniqueConstraint(
                fields=["user", "station"], name="unique_review_per_user_station"
            ),
        ),
        migrations.AddConstraint(
            model_name="review",
            constraint=models.CheckConstraint(
                check=models.Q(rating__gte=1) & models.Q(rating__lte=5),
                name="review_rating_range",
            ),
        ),
    ]
