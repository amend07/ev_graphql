"""Owner-decision provenance: who approved/rejected, when, and why (Sprint B2.1).

**No data backfill, deliberately — and this is the opposite call to 0005.**

0005 had to backfill because 0004's new column *changed the meaning of existing
rows*: every live owner would have defaulted to un-approved and silently lost
station management on deploy. These three columns gate nothing. `reviewer` NULL
and `reviewed_at` NULL mean "we do not know who decided this, or when", which is
the truth for every decision taken before this migration — including the
approvals 0005 itself granted, which no admin ever made.

Inventing a reviewer to make the column look populated would put a name against a
decision that person did not take, in the exact field the platform keeps to
answer "who decided this". An empty `rejection_reason` on an owner rejected under
B1 is accurate for the same reason: B1 accepted rejections with no reason, so
there is none to recover. The gap is real and the record should say so.

Also widens AuditLog.action's choices for the station/review actions B2.1 adds.
Choices are validated in Python, not by the database, so no existing row changes.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_backfill_owner_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='rejection_reason',
            field=models.TextField(blank=True, default='', help_text="Why the owner application was rejected. Empty unless owner_status='rejected'."),
        ),
        migrations.AddField(
            model_name='user',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, help_text='When owner_status was last decided by an admin.', null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='reviewer',
            field=models.ForeignKey(blank=True, help_text='Admin who approved or rejected this station owner. NULL if never reviewed.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='owner_decisions', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(choices=[('user_activated', 'User activated'), ('user_deactivated', 'User deactivated'), ('user_deleted', 'User deleted'), ('owner_approved', 'Station owner approved'), ('owner_rejected', 'Station owner rejected'), ('station_activated', 'Station activated'), ('station_deactivated', 'Station deactivated'), ('review_hidden', 'Review hidden'), ('review_restored', 'Review restored'), ('review_deleted', 'Review deleted')], max_length=40),
        ),
    ]
