"""Grant administrator privilege to an existing account (Sprint B3, Priority 4).

    manage.py promote_admin <username>

Replaces `create_admin_user.py`, an interactive script at the repo root that
created an admin from scratch, wrote no audit record, and skipped the credential
policy every other account goes through. The single action that mints
platform-wide privilege was the only one leaving no trace.

The split is the point: accounts are created through the normal, validated,
rate-limited API like everyone else's, and this grants privilege to one that
already exists. One way to make an account; one way to make it powerful; both
recorded.

WHY THIS IS NOT A GraphQL MUTATION — see `administration.promote_to_admin`. In
short: an API that grants admin turns a stolen admin session into permanent
persistence, and requiring server access is the safeguard, not an inconvenience.

Safeguards here:
* Runs through the service layer, so the AuditLog entry is unskippable.
* Idempotent — promoting an admin twice is a no-op, not a second record.
* --dry-run, because the destructive-looking cousin of this command is one typo
  away from promoting the wrong person.
* Records the operating-system user, which is the only identity a shell has.
"""

import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts import administration

User = get_user_model()


class Command(BaseCommand):
    help = "Grant administrator privilege to an existing user account."

    def add_arguments(self, parser):
        parser.add_argument('username', help="Username of the account to promote.")
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report what would change and exit without writing.",
        )

    def handle(self, *args, **options):
        username = options['username']
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            # A typo must not silently create an account, and must not look like
            # success. Register through the API first, then promote.
            raise CommandError(
                f"No account with username {username!r}. This command promotes an "
                f"EXISTING account; it does not create one."
            )

        if user.role == 'admin':
            self.stdout.write(f"{username} is already an administrator. Nothing to do.")
            return

        if not user.is_active:
            # Deactivated accounts cannot authenticate at all, so promoting one
            # produces an admin who cannot sign in — a confusing non-event.
            raise CommandError(
                f"{username} is deactivated. Reactivate the account before "
                f"promoting it, or the new administrator cannot sign in."
            )

        if options['dry_run']:
            self.stdout.write(
                f"DRY RUN: would promote {username} ({user.email}) "
                f"from role={user.role!r} to 'admin'. Nothing was written."
            )
            return

        operator = getpass.getuser()
        administration.promote_to_admin(
            target=user, via=f'manage.py promote_admin (operator: {operator})',
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{username} is now an administrator. Recorded in the audit log."
            )
        )
