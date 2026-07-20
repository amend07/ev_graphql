"""Display name (Sprint W8).

The executable form of §10a of docs/IDENTITY_ARCHITECTURE.md: the gate that had
to close before an SMS provider could be configured.

The defect these tests exist to prevent is specific and was written down before
it could happen: a phone-registered account has no username it chose, so
`identity._generate_username` mints `phone_9f2c1a4b…`, and both clients rendered
that string at the user — "Welcome back, phone_9f2c1a4b8e3d0f11". It was
invisible only because phone sign-in was switched off.

Written under rule 13 ("a guard you have not seen fail is not a guard"): each
assertion below was watched failing — by making `display_label` return
`self.username` — before it was allowed to pass.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from graphql_jwt.shortcuts import get_token

from ev_backend.schema import schema

from . import identity
from .models import AuthIdentity
from .phone import mask_phone

User = get_user_model()

PHONE = '+251911223344'


class Context:
    """A request stand-in; resolvers read `user`."""

    def __init__(self, user=None):
        self.user = user
        self.META = {'REMOTE_ADDR': '198.51.100.7'}


def phone_account():
    """An account created the way a real phone signup creates one."""
    user, _created = identity.sign_in_with_identity(
        provider=AuthIdentity.PROVIDER_PHONE, subject=PHONE
    )
    return user


class DisplayLabelFallback(TestCase):
    """`User.display_label` — the single place the fallback order is decided."""

    def test_the_chosen_name_wins_when_it_is_set(self):
        user = phone_account()
        user.display_name = 'Selam'
        user.save(update_fields=['display_name'])

        self.assertEqual(user.display_label, 'Selam')

    def test_a_phone_account_with_no_chosen_name_reads_as_its_masked_number(self):
        # The §10a case. Not the raw number either: this string is rendered on a
        # dashboard, and a shoulder-surfer should not read a full phone number
        # off it.
        user = phone_account()

        self.assertEqual(user.display_label, mask_phone(PHONE))
        self.assertNotIn('911223344', user.display_label)

    def test_it_never_returns_the_generated_username_artefact(self):
        # THE test. Pinned against the real generator rather than a hardcoded
        # 'phone_…' string, so that renaming the prefix in identity.py cannot
        # quietly make this assertion vacuous while the bug returns.
        user = phone_account()
        artefact = identity._generate_username(AuthIdentity.PROVIDER_PHONE, PHONE)

        self.assertEqual(user.username, artefact)  # the artefact really is there
        self.assertNotEqual(user.display_label, artefact)
        for prefix in User._GENERATED_USERNAME_PREFIXES:
            self.assertFalse(user.display_label.startswith(prefix))

    def test_a_legacy_username_account_reads_as_the_username_it_typed(self):
        # Pre-W7 accounts chose their username. It is a real name to them, and
        # replacing it with 'User' would be a regression for every existing user.
        user = User.objects.create_user(username='selam', email='s@example.com', password='x')

        self.assertEqual(user.display_label, 'selam')

    def test_a_provider_account_with_no_phone_falls_back_to_something_sayable(self):
        # An OAuth signup: minted username, no phone. There is nothing true to
        # show, so it must at least not be the artefact.
        user = identity.sign_in_with_identity(
            provider=AuthIdentity.PROVIDER_GOOGLE, subject='google-sub-1'
        )[0]

        self.assertEqual(user.display_label, 'User')

    def test_it_is_always_a_non_empty_string(self):
        # The GraphQL field is non-null; a client rendering `displayName` must
        # never get '' and print an empty greeting.
        for user in (
            phone_account(),
            User.objects.create_user(username='typed', email='t@example.com', password='x'),
        ):
            self.assertTrue(user.display_label)


class PublicDisplayLabel(TestCase):
    """`public_display_label` — what strangers see. The phone step is absent.

    This is the one place the two labels must NOT agree, so each test states
    which of them it is pinning.
    """

    def test_a_phone_account_is_anonymous_in_public_not_a_masked_number(self):
        # The trade this property exists to make. `display_label` shows the
        # masked number on your own dashboard, which is useful; the same string
        # under a public review is the last four digits of a real phone number
        # published next to whatever that review says.
        user = phone_account()

        self.assertEqual(user.display_label, mask_phone(PHONE))  # private: masked number
        self.assertEqual(user.public_display_label, 'User')  # public: nothing
        self.assertNotIn('3344', user.public_display_label)

    def test_a_chosen_name_is_shown_in_public_because_they_chose_it(self):
        user = phone_account()
        user.display_name = 'Selam'
        user.save(update_fields=['display_name'])

        self.assertEqual(user.public_display_label, 'Selam')

    def test_it_never_returns_the_generated_username_artefact(self):
        user = phone_account()

        self.assertNotEqual(user.public_display_label, user.username)
        for prefix in User._GENERATED_USERNAME_PREFIXES:
            self.assertFalse(user.public_display_label.startswith(prefix))

    def test_a_legacy_username_account_keeps_its_public_name(self):
        user = User.objects.create_user(username='selam', email='s@example.com', password='x')

        self.assertEqual(user.public_display_label, 'selam')


class DisplayNameOverGraphQL(TestCase):
    def test_me_exposes_display_name_and_not_the_artefact(self):
        user = phone_account()
        result = schema.execute(
            '{ me { displayName username } }', context_value=Context(user=user)
        )

        self.assertIsNone(result.errors)
        self.assertEqual(result.data['me']['displayName'], mask_phone(PHONE))
        # `username` is still selectable for the legacy clients that sign in with
        # one; the point is that `displayName` is the field to render.
        self.assertTrue(result.data['me']['username'].startswith('phone_'))

    def test_set_display_name_writes_it_and_is_reflected_immediately(self):
        user = phone_account()
        result = schema.execute(
            'mutation($n:String!){ setDisplayName(displayName:$n){ success user { displayName } } }',
            variable_values={'n': 'Selam'},
            context_value=Context(user=user),
        )

        self.assertIsNone(result.errors)
        self.assertTrue(result.data['setDisplayName']['success'])
        self.assertEqual(result.data['setDisplayName']['user']['displayName'], 'Selam')
        user.refresh_from_db()
        self.assertEqual(user.display_name, 'Selam')

    def test_set_display_name_requires_a_signed_in_user(self):
        result = schema.execute(
            'mutation{ setDisplayName(displayName:"Selam"){ success } }',
            context_value=Context(user=None),
        )

        self.assertIsNotNone(result.errors)

    def test_clearing_the_name_falls_back_to_the_masked_number_again(self):
        # Legitimate, not an error: a user may want their name off the screen.
        user = phone_account()
        user.display_name = 'Selam'
        user.save(update_fields=['display_name'])

        result = schema.execute(
            'mutation{ setDisplayName(displayName:""){ user { displayName } } }',
            context_value=Context(user=user),
        )

        self.assertIsNone(result.errors)
        self.assertEqual(result.data['setDisplayName']['user']['displayName'], mask_phone(PHONE))

    def test_whitespace_is_collapsed_rather_than_stored_verbatim(self):
        user = phone_account()
        schema.execute(
            'mutation{ setDisplayName(displayName:"  Selam   Bekele  "){ success } }',
            context_value=Context(user=user),
        )
        user.refresh_from_db()

        self.assertEqual(user.display_name, 'Selam Bekele')

    def test_create_user_accepts_a_display_name(self):
        result = schema.execute(
            'mutation{ createUser(username:"selam", email:"s@example.com", pin:"123456",'
            ' displayName:"Selam"){ success user { displayName } } }',
            context_value=Context(user=None),
        )

        self.assertIsNone(result.errors)
        self.assertEqual(result.data['createUser']['user']['displayName'], 'Selam')

    def test_create_user_without_one_still_reads_as_the_typed_username(self):
        result = schema.execute(
            'mutation{ createUser(username:"selam", email:"s@example.com", pin:"123456")'
            '{ success user { displayName } } }',
            context_value=Context(user=None),
        )

        self.assertIsNone(result.errors)
        self.assertEqual(result.data['createUser']['user']['displayName'], 'selam')
