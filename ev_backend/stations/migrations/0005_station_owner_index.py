from django.db import migrations, models


class Migration(migrations.Migration):
    """Composite index backing the my_stations (owner + is_active) query."""

    dependencies = [
        ("stations", "0004_business_integrity"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="station",
            index=models.Index(fields=["owner", "is_active"], name="station_owner_active_idx"),
        ),
    ]
