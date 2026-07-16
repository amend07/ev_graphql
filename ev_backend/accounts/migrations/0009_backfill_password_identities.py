"""Give every pre-W7 account an AuthIdentity for the login it already has.

Without this, migration 0008 leaves every existing user with ZERO linked
identities — and the "you cannot unlink your last identity" rule would then read
those accounts as having nothing to protect, while the identity list on the
profile screen would show a person who signs in every day that they have no way
to sign in.

The point of the backfill is conceptual, not cosmetic: after it, today's
username+PIN login is *one provider among several* rather than a special case the
rest of the identity system has to carve exceptions for. Everything downstream —
linking, unlinking, the last-identity rule — then works uniformly for legacy and
new accounts alike.

`subject` is the username because that is what `tokenAuth` authenticates against
today. `verified_at` is NULL: the account exists and the password works, but
nobody ever proved the *username* belongs to that human, and recording a
verification that never happened would be a lie the linking rules would later
trust.

Same shape as B1's migration 0005 (which grandfathered live owners to
'approved'): a schema change whose data half is what stops it breaking
production.

Reverse: delete only the rows this created. Nothing else is touched.
"""

from django.db import migrations


def backfill_password_identities(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    AuthIdentity = apps.get_model('accounts', 'AuthIdentity')

    existing = set(
        AuthIdentity.objects.filter(provider='password').values_list('user_id', flat=True)
    )
    # bulk_create in one round trip: this runs against every row in the user
    # table, and a per-user save would make deploy time scale with signups.
    AuthIdentity.objects.bulk_create(
        [
            AuthIdentity(
                user_id=user.id,
                provider='password',
                subject=user.username,
                verified_at=None,
            )
            for user in User.objects.exclude(id__in=existing).only('id', 'username')
        ],
        # Belt and braces against the UNIQUE(provider, subject) constraint if a
        # partially-applied run is retried.
        ignore_conflicts=True,
    )


def remove_password_identities(apps, schema_editor):
    AuthIdentity = apps.get_model('accounts', 'AuthIdentity')
    AuthIdentity.objects.filter(provider='password').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_w7_identity_model'),
    ]

    operations = [
        migrations.RunPython(backfill_password_identities, remove_password_identities),
    ]
