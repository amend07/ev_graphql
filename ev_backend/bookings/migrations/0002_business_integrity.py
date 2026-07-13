from django.db import migrations, models


class Migration(migrations.Migration):
    """Booking integrity (Sprint 4): end-after-start check constraint and
    indexes supporting overlap/availability and ownership queries. Additive."""

    dependencies = [
        ("bookings", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="booking",
            constraint=models.CheckConstraint(
                check=models.Q(end_time__gt=models.F("start_time")),
                name="booking_end_after_start",
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(fields=["station", "status"], name="booking_station_status_idx"),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(fields=["station", "start_time", "end_time"], name="booking_station_time_idx"),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(fields=["user", "status"], name="booking_user_status_idx"),
        ),
    ]
