"""Review moderation flag (Sprint B2.1).

**The backfill is the column default, and that is the whole point.**

`is_hidden` defaults to False, so every review that exists when this runs stays
visible and keeps counting toward its station's rating — which is exactly the
behaviour it has today. Nothing a customer or an owner sees changes on deploy;
the only thing that changes is that a moderator now has a lever.

The 0005 failure mode (a new column silently changing what existing rows mean)
is avoided here by choosing the default that preserves current behaviour, rather
than by a RunPython pass. The dangerous default would have been `True`.

The index matches the filter every public read now applies (station + is_hidden),
so moderation does not cost the discovery path a sequential scan.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stations', '0005_station_owner_index'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='review',
            name='is_hidden',
            field=models.BooleanField(default=False, help_text='Hidden by a moderator: invisible to the public and excluded from ratings.'),
        ),
        migrations.AddIndex(
            model_name='review',
            index=models.Index(fields=['station', 'is_hidden'], name='review_station_hidden_idx'),
        ),
    ]
