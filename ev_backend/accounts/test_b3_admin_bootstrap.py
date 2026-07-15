"""Admin bootstrap (Sprint B3, Priority 4).

Covers `manage.py promote_admin` and the decision it encodes: administrator
privilege is grantable only from the server, never over the API.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase
from graphene.test import Client

from ev_backend.schema import schema

from .models import AuditLog

User = get_user_model()


def make_user(username, **kw):
    return User.objects.create_user(
        username=username, email=f'{username}@x.com', password='123456', **kw,
    )


class PromoteAdminCommand(TestCase):
    def setUp(self):
        self.user = make_user('candidate')
        self.out = StringIO()

    def test_it_promotes_an_existing_account(self):
        call_command('promote_admin', 'candidate', stdout=self.out)

        self.user.refresh_from_db()
        self.assertEqual(self.user.role, 'admin')
        self.assertTrue(
            self.user.is_staff,
            "role and is_staff must move together: Django's own permission checks "
            "read is_staff, ours read role.",
        )

    def test_it_records_who_and_how_in_the_audit_log(self):
        call_command('promote_admin', 'candidate', stdout=self.out)

        entry = AuditLog.objects.get(action=AuditLog.ACTION_ADMIN_PROMOTED)
        self.assertEqual(entry.target_label, 'candidate')
        self.assertEqual(entry.metadata['previous_role'], 'user')
        # No one is signed in during a shell run, so the trail records the OS
        # operator instead of pretending an actor.
        self.assertIn('promote_admin', entry.metadata['via'])
        self.assertIsNone(entry.actor)

    def test_it_refuses_an_unknown_username_rather_than_creating_one(self):
        with self.assertRaises(CommandError) as ctx:
            call_command('promote_admin', 'ghost', stdout=self.out)
        self.assertIn('does not create', str(ctx.exception))
        self.assertFalse(User.objects.filter(username='ghost').exists())

    def test_it_refuses_a_deactivated_account(self):
        make_user('dormant', is_active=False)
        with self.assertRaises(CommandError):
            call_command('promote_admin', 'dormant', stdout=self.out)
        self.assertEqual(User.objects.get(username='dormant').role, 'user')

    def test_it_is_idempotent_and_writes_no_second_record(self):
        call_command('promote_admin', 'candidate', stdout=self.out)
        call_command('promote_admin', 'candidate', stdout=self.out)

        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.ACTION_ADMIN_PROMOTED).count(), 1,
        )
        self.assertIn('already an administrator', self.out.getvalue())

    def test_dry_run_writes_nothing(self):
        call_command('promote_admin', 'candidate', '--dry-run', stdout=self.out)

        self.user.refresh_from_db()
        self.assertEqual(self.user.role, 'user')
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertIn('DRY RUN', self.out.getvalue())


class PrivilegeIsNotGrantableOverTheApi(TestCase):
    """The security property this sprint chose to keep.

    A stolen admin session buys damage today. It must not also buy persistence —
    an attacker minting a second admin that survives revoking the first.
    """

    def test_the_schema_exposes_no_way_to_change_a_role(self):
        mutations = set(schema.graphql_schema.type_map['Mutation'].fields)
        for forbidden in ('promoteToAdmin', 'setUserRole', 'updateUserRole', 'demoteAdmin'):
            self.assertNotIn(
                forbidden, mutations,
                f"{forbidden} would make admin privilege grantable over the network. "
                f"See accounts.administration.promote_to_admin for why it is not.",
            )

    def test_no_mutation_field_writes_role(self):
        # Belt and braces: the guard above names fields, this one checks the
        # capability. `createUser` picks a role from a boolean, but only between
        # customer and station_owner — never admin.
        request = RequestFactory().post('/graphql/')
        request.user = AnonymousUser()
        result = Client(schema).execute(
            '''
            mutation { createUser(username: "sneak", email: "s@x.com", pin: "123456",
                                  isStationOwner: true) { user { id role } } }
            ''',
            context=request,
        )
        self.assertIsNone(result.get('errors'))
        self.assertNotEqual(result['data']['createUser']['user']['role'], 'admin')
        self.assertFalse(User.objects.filter(role='admin').exists())
