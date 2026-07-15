"""Backfill owner_status for accounts that predate the approval lifecycle.

Every station owner that exists when this runs was created under the old rule
(owners were active immediately and `approveStationOwner` was a no-op), so they
are already operating: they have live stations and real bookings. Leaving them at
the new NULL/pending default would silently revoke station management from every
existing owner on deploy. They are grandfathered to 'approved'.

Non-owners keep NULL — the field only means something for station owners.

Reverse: clear the field. The column drop is handled by the schema migration, so
there is nothing else to undo.
"""

from django.db import migrations


def approve_existing_owners(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.filter(role='station_owner').update(owner_status='approved')


def clear_owner_status(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.update(owner_status=None)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_owner_approval_and_audit_log'),
    ]

    operations = [
        migrations.RunPython(approve_existing_owners, clear_owner_status),
    ]
