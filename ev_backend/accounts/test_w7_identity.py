"""Identity & authentication (Sprint W7).

The §3 linking-rules table from docs/IDENTITY_ARCHITECTURE.md, executable. Each
rule in that table is a test here, because a rule that is only written down is a
rule that is only true until someone refactors.

Written under rule 13 ("a guard you have not seen fail is not a guard"): every
assertion below was watched failing before it was allowed to pass. The three
vacuous tests caught in W5 and W6 all shared a shape — they asserted something
the framework already guaranteed — and the antidote is to break the guard on
purpose and confirm the test notices.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from graphql_jwt.shortcuts import get_token

from ev_backend.errors import Conflict, NotFound, ValidationError
from ev_backend.schema import schema

from . import identity, phone_service
from .jwt import TOKEN_VERSION_CLAIM, TokenRevoked, jwt_decode, jwt_payload
from .models import AuthIdentity, PhoneVerification
from .phone import mask_phone, normalize_phone
from .sms import DisabledSmsBackend, SmsUnavailable, get_sms_backend, sms_configured

User = get_user_model()


class Context:
    """A request stand-in. Rate limiting reads META; resolvers read `user`."""

    def __init__(self, user=None, ip='198.51.100.7'):
        self.user = user
        self.META = {'REMOTE_ADDR': ip}


def clear_rate_limits():
    from django.core.cache import cache

    cache.clear()


# ── E.164 normalisation ──────────────────────────────────────────────────────


class PhoneNormalization(TestCase):
    """One number, one representation — the premise the UNIQUE constraint rests on."""

    def test_every_way_a_human_writes_one_number_normalises_to_the_same_string(self):
        # The whole point: if any of these disagreed, the same handset could hold
        # two accounts and "one human, one account" would be decoration.
        for raw in [
            '+251911223344',
            '251911223344',
            '0911223344',
            '0911 22 33 44',
            '+251-911-223344',
            '(0911) 223344',
            '00251911223344',
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), '+251911223344')

    def test_trunk_prefix_is_stripped_not_kept(self):
        # The classic bug: '0911...' -> '+2510911...' is plausible-looking, is a
        # different number, and would sit in the table next to the real one.
        self.assertEqual(normalize_phone('0911223344'), '+251911223344')
        self.assertNotEqual(normalize_phone('0911223344'), '+2510911223344')

    def test_junk_is_rejected(self):
        for raw in ['', '   ', 'not a phone', '+', '123', '+251' + '9' * 20, '09+11223344']:
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    normalize_phone(raw)

    def test_mask_keeps_only_the_last_four(self):
        masked = mask_phone('+251911223344')
        self.assertTrue(masked.endswith('3344'))
        self.assertNotIn('911', masked.replace('+251', ''))
        self.assertEqual(len(masked), len('+251911223344'))


# ── The §3 linking rules ─────────────────────────────────────────────────────


class LinkingRules(TestCase):
    """docs/IDENTITY_ARCHITECTURE.md §3, one test per row."""

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', email='alice@example.com', password='123456')
        self.bob = User.objects.create_user(username='bob', email='bob@example.com', password='123456')

    def test_an_unclaimed_identity_links(self):
        row = identity.link_identity(user=self.alice, provider='google', subject='g-1')
        self.assertEqual(row.user, self.alice)
        self.assertIsNotNone(row.verified_at)

    def test_an_identity_already_held_by_someone_else_is_refused(self):
        identity.link_identity(user=self.bob, provider='google', subject='g-1')
        with self.assertRaises(Conflict):
            identity.link_identity(user=self.alice, provider='google', subject='g-1')

    def test_the_refusal_does_not_name_the_other_account(self):
        # Otherwise this is an oracle: "is bob@example.com on this platform, and
        # does he use Google?" — answerable by anyone, one request at a time.
        identity.link_identity(user=self.bob, provider='google', subject='g-1')
        with self.assertRaises(Conflict) as caught:
            identity.link_identity(user=self.alice, provider='google', subject='g-1')
        message = str(caught.exception)
        self.assertNotIn('bob', message)
        self.assertNotIn('bob@example.com', message)

    def test_a_second_identity_from_the_same_provider_is_refused(self):
        identity.link_identity(user=self.alice, provider='google', subject='g-1')
        with self.assertRaises(Conflict):
            identity.link_identity(user=self.alice, provider='google', subject='g-2')

    def test_different_providers_may_share_a_subject_string(self):
        # Google's 'x' and Apple's 'x' are different people. The constraint is on
        # the PAIR; a UNIQUE on subject alone would collide them.
        identity.link_identity(user=self.alice, provider='google', subject='same')
        identity.link_identity(user=self.alice, provider='apple', subject='same')
        self.assertEqual(self.alice.identities.count(), 2)

    def test_unlinking_the_last_identity_is_refused(self):
        identity.link_identity(user=self.alice, provider='google', subject='g-1')
        with self.assertRaises(Conflict):
            identity.unlink_identity(user=self.alice, provider='google')

    def test_unlinking_is_allowed_once_another_identity_exists(self):
        identity.link_identity(user=self.alice, provider='google', subject='g-1')
        identity.link_identity(user=self.alice, provider='apple', subject='a-1')
        identity.unlink_identity(user=self.alice, provider='google')
        self.assertEqual([i.provider for i in self.alice.identities.all()], ['apple'])

    def test_unlinking_the_canonical_phone_is_refused(self):
        identity.attach_phone(user=self.alice, phone_e164='+251911223344')
        identity.link_identity(user=self.alice, provider='google', subject='g-1')
        # Not the last identity — so this refusal is the phone rule, not the
        # last-identity rule. Without the distinction the test would pass for the
        # wrong reason.
        with self.assertRaises(Conflict):
            identity.unlink_identity(user=self.alice, provider='phone')

    def test_unlinking_something_not_linked_is_not_found(self):
        with self.assertRaises(NotFound):
            identity.unlink_identity(user=self.alice, provider='google')


class EmailIsNeverAnIdentity(TestCase):
    """The takeover primitive §3 exists to prevent.

    Our stored emails were NEVER verified — `createUser` accepts any string. So
    "Google says this email, we have a user with it, log them in" means: register
    with a victim's address, wait, sign in with Google, inherit their account.
    """

    def test_signing_in_with_a_provider_never_adopts_an_account_by_matching_email(self):
        victim = User.objects.create_user(
            username='victim', email='victim@example.com', password='123456'
        )

        # The attacker controls a Google account with the victim's email address.
        user, created = identity.sign_in_with_identity(
            provider='google', subject='attacker-google-sub', email='victim@example.com'
        )

        self.assertTrue(created, "matched an existing account by email — this is account takeover")
        self.assertNotEqual(user.pk, victim.pk)
        self.assertEqual(User.objects.filter(email='victim@example.com').count(), 2)

    def test_the_same_subject_returns_to_the_same_account(self):
        first, created_first = identity.sign_in_with_identity(provider='google', subject='g-1')
        second, created_second = identity.sign_in_with_identity(provider='google', subject='g-1')
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)

    def test_a_generated_username_never_contains_the_subject(self):
        # The subject may be a phone number, and `usersPage(search:)` matches on
        # username — so a raw subject there would make numbers searchable by admins
        # and leak the identifier into a field that is echoed back.
        user, _ = identity.sign_in_with_identity(provider='phone', subject='+251911223344')
        self.assertNotIn('251911223344', user.username)
        self.assertNotIn('911223344', user.username)


class PhoneAttachment(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', email='a@example.com', password='123456')
        self.bob = User.objects.create_user(username='bob', email='b@example.com', password='123456')

    def test_attaching_sets_the_number_the_proof_and_the_identity(self):
        identity.attach_phone(user=self.alice, phone_e164='+251911223344')
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.phone_e164, '+251911223344')
        self.assertIsNotNone(self.alice.phone_verified_at)
        self.assertTrue(self.alice.has_verified_phone())
        self.assertTrue(self.alice.identities.filter(provider='phone').exists())

    def test_a_number_on_another_account_is_refused(self):
        identity.attach_phone(user=self.bob, phone_e164='+251911223344')
        with self.assertRaises(Conflict):
            identity.attach_phone(user=self.alice, phone_e164='+251911223344')

    def test_the_refusal_does_not_name_the_holder(self):
        identity.attach_phone(user=self.bob, phone_e164='+251911223344')
        with self.assertRaises(Conflict) as caught:
            identity.attach_phone(user=self.alice, phone_e164='+251911223344')
        self.assertNotIn('bob', str(caught.exception))

    def test_changing_your_own_number_moves_the_identity_with_it(self):
        identity.attach_phone(user=self.alice, phone_e164='+251911223344')
        identity.attach_phone(user=self.alice, phone_e164='+251922334455')
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.phone_e164, '+251922334455')
        # One phone identity, pointing at the new number — not two.
        rows = self.alice.identities.filter(provider='phone')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().subject, '+251922334455')


# ── Legacy accounts keep working ─────────────────────────────────────────────


class LegacyAccountsAreNotBroken(TestCase):
    """The non-negotiable constraint: existing accounts continue working."""

    def test_a_pre_w7_account_has_no_phone_and_that_is_valid(self):
        legacy = User.objects.create_user(username='legacy', email='l@example.com', password='123456')
        self.assertIsNone(legacy.phone_e164)
        self.assertIsNone(legacy.phone_verified_at)
        self.assertFalse(legacy.has_verified_phone())

    def test_many_accounts_may_have_no_phone_at_once(self):
        # NULL is distinct under UNIQUE in both SQLite and PostgreSQL, but this
        # is the assumption the whole additive migration rests on, so it is
        # asserted rather than believed: if UNIQUE treated NULLs as equal, the
        # second signup on a fresh deploy would fail.
        User.objects.create_user(username='u1', email='u1@example.com', password='123456')
        User.objects.create_user(username='u2', email='u2@example.com', password='123456')
        self.assertEqual(User.objects.filter(phone_e164__isnull=True).count(), 2)

    def test_username_and_pin_sign_in_still_works_after_w7(self):
        User.objects.create_user(username='legacy', email='l@example.com', password='654321')
        clear_rate_limits()
        result = schema.execute(
            '''
            mutation { tokenAuth(username: "legacy", pin: "654321") { token user { username } } }
            ''',
            context=Context(),
        )
        self.assertIsNone(result.errors)
        self.assertIsNotNone(result.data['tokenAuth']['token'])


class BackfillMigration(TestCase):
    """Migration 0009 gives every pre-W7 account the identity it already had.

    Runs the migration's OWN function rather than a re-implementation of it —
    a test that reimplements the thing under test only proves it can be written
    twice. `importlib` because the module name starts with a digit and cannot be
    imported with an `import` statement; `django.apps` because it satisfies the
    same `.get_model` interface RunPython passes in.
    """

    def setUp(self):
        import importlib

        self.migration = importlib.import_module(
            'accounts.migrations.0009_backfill_password_identities'
        )
        self.users = [
            User.objects.create_user(username=f'legacy{i}', email=f'l{i}@example.com', password='123456')
            for i in range(3)
        ]
        # create_user does not create identities; the backfill is what does.
        AuthIdentity.objects.all().delete()

    def _run(self):
        from django.apps import apps as live_apps

        self.migration.backfill_password_identities(live_apps, None)

    def test_every_existing_account_gets_a_password_identity(self):
        self._run()
        for user in self.users:
            with self.subTest(user=user.username):
                row = AuthIdentity.objects.get(user=user, provider='password')
                self.assertEqual(row.subject, user.username)

    def test_the_backfilled_identity_is_not_marked_verified(self):
        # The account exists and the PIN works, but nobody ever proved the
        # USERNAME belongs to that human. Recording a verification that never
        # happened would be a lie the linking rules would later trust.
        self._run()
        self.assertEqual(AuthIdentity.objects.filter(verified_at__isnull=False).count(), 0)

    def test_running_it_twice_changes_nothing(self):
        # Deploys get retried. A backfill that is not idempotent turns a retry
        # into an IntegrityError and a half-migrated database.
        self._run()
        self._run()
        self.assertEqual(AuthIdentity.objects.filter(provider='password').count(), len(self.users))

    def test_after_backfill_a_legacy_user_is_not_stranded_by_the_last_identity_rule(self):
        # The point of the backfill, stated as behaviour: before it, a legacy
        # user has zero identities, and the profile screen would tell someone who
        # signs in daily that they have no way to sign in.
        self._run()
        user = self.users[0]
        self.assertEqual(user.identities.count(), 1)
        with self.assertRaises(Conflict):
            identity.unlink_identity(user=user, provider='password')

    def test_reverse_removes_only_what_it_created(self):
        self._run()
        identity.link_identity(user=self.users[0], provider='google', subject='g-1')

        from django.apps import apps as live_apps

        self.migration.remove_password_identities(live_apps, None)

        self.assertEqual(AuthIdentity.objects.filter(provider='password').count(), 0)
        self.assertEqual(AuthIdentity.objects.filter(provider='google').count(), 1)


# ── Session revocation (§5b) ─────────────────────────────────────────────────


class TokenVersionRevocation(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', email='a@example.com', password='123456')

    def test_a_fresh_token_carries_the_users_current_generation(self):
        payload = jwt_payload(self.user)
        self.assertEqual(payload[TOKEN_VERSION_CLAIM], self.user.token_version)

    def test_a_token_still_decodes_before_any_revocation(self):
        token = get_token(self.user)
        self.assertEqual(jwt_decode(token)['username'], 'alice')

    def test_revoking_rejects_every_token_issued_before_it(self):
        token = get_token(self.user)
        identity.revoke_all_sessions(user=self.user)
        with self.assertRaises(TokenRevoked):
            jwt_decode(token)

    def test_a_token_issued_after_revoking_works(self):
        identity.revoke_all_sessions(user=self.user)
        self.user.refresh_from_db()
        self.assertEqual(jwt_decode(get_token(self.user))['username'], 'alice')

    def test_revoking_one_user_does_not_touch_another(self):
        bob = User.objects.create_user(username='bob', email='b@example.com', password='123456')
        bob_token = get_token(bob)
        identity.revoke_all_sessions(user=self.user)
        self.assertEqual(jwt_decode(bob_token)['username'], 'bob')

    def test_a_pre_w7_token_without_the_claim_still_works(self):
        # THE MIGRATION SAFETY PROPERTY. Every token issued before this deploy
        # has no `tv` claim. If a missing claim were treated as invalid, shipping
        # W7 would sign out every user on the platform at once.
        from graphql_jwt.utils import jwt_encode

        legacy = jwt_encode({'username': 'alice', 'exp': _future_exp()})
        self.assertEqual(jwt_decode(legacy)['username'], 'alice')

    def test_a_pre_w7_token_stops_working_once_that_user_revokes(self):
        # The other half: legacy tokens are grandfathered, but revocation still
        # has to reach them, or "log me out everywhere" would miss the oldest
        # and most likely-stolen sessions.
        from graphql_jwt.utils import jwt_encode

        legacy = jwt_encode({'username': 'alice', 'exp': _future_exp()})
        identity.revoke_all_sessions(user=self.user)
        with self.assertRaises(TokenRevoked):
            jwt_decode(legacy)

    def test_revocation_reaches_the_refresh_path(self):
        # The one that makes revocation real. `JWT_ALLOW_REFRESH` is on, so if
        # `refreshToken` did not decode through this handler, a revoked token
        # could mint a fresh valid one forever and revocation would revoke
        # nothing. Asserted through the actual mutation, not the handler.
        token = get_token(self.user)
        identity.revoke_all_sessions(user=self.user)
        result = schema.execute(
            'mutation Refresh($t: String!) { refreshToken(token: $t) { token } }',
            variables={'t': token},
            context=Context(),
        )
        self.assertIsNotNone(result.errors, "a revoked token was refreshed into a valid one")

    def test_a_revoked_token_is_rejected_with_the_code_clients_sign_out_on(self):
        # The Flutter client's _isAuthError matches `extensions.code ==
        # 'unauthenticated'`. Without that code the server refuses the token and
        # the client keeps it forever, never signing the user out — a revocation
        # the victim cannot see.
        self.assertEqual(TokenRevoked.code, 'unauthenticated')
        from ev_backend.errors import APIError
        from graphql_jwt.exceptions import JSONWebTokenError

        # Both parents are load-bearing; see accounts/jwt.py.
        self.assertIsInstance(TokenRevoked(), APIError)
        self.assertIsInstance(TokenRevoked(), JSONWebTokenError)

    def test_the_revoked_message_does_not_claim_expiry(self):
        self.assertNotIn('expire', str(TokenRevoked()).lower())


def _future_exp():
    from calendar import timegm

    return timegm((timezone.now() + timedelta(minutes=30)).utctimetuple())


# ── SMS delivery: the honesty rule ───────────────────────────────────────────


class SmsBackendHonesty(TestCase):
    def test_the_default_refuses_rather_than_pretending(self):
        # A flow whose OTP silently goes nowhere is worse than a disabled button:
        # the user waits for a code that was never sent and support cannot say why.
        self.assertIsInstance(get_sms_backend(), DisabledSmsBackend)
        self.assertFalse(sms_configured())
        with self.assertRaises(SmsUnavailable):
            get_sms_backend().send(to_e164='+251911223344', body='x')

    @override_settings(SMS_BACKEND='console')
    def test_console_is_selectable_for_development(self):
        self.assertTrue(sms_configured())

    @override_settings(SMS_BACKEND='definitely-not-a-backend')
    def test_an_unknown_backend_fails_closed(self):
        # A deploy typo must not silently drop verification codes.
        self.assertIsInstance(get_sms_backend(), DisabledSmsBackend)

    def test_sms_unavailable_is_typed_so_a_client_can_explain_itself(self):
        self.assertEqual(SmsUnavailable.code, 'sms_unavailable')


class ProductionRefusesConsoleSms(TestCase):
    def test_settings_reject_a_console_backend_on_a_real_deploy(self):
        # ConsoleSmsBackend logs the code in clear text. On a live deploy that
        # writes a live credential into the log aggregator while the user
        # believes their phone is a second factor.
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / 'ev_backend' / 'settings.py'
        text = source.read_text()
        self.assertTrue(
            re.search(r"if SMS_BACKEND == 'console':\s*\n\s*_problems\.append", text),
            "the production check no longer refuses SMS_BACKEND='console'",
        )


# ── Phone verification ───────────────────────────────────────────────────────


@override_settings(SMS_BACKEND='console')
class PhoneVerificationFlow(TestCase):
    def setUp(self):
        clear_rate_limits()

    def _send_and_capture_code(self, raw='0911223344'):
        """Send, and recover the plaintext code the way only a test may.

        Patches the adapter rather than reading the database, because the hash is
        all the database has — which is the property being relied on.
        """
        sent = {}

        def capture(*, to_e164, body):
            sent['to'] = to_e164
            sent['body'] = body

        with patch('accounts.phone_service.get_sms_backend') as backend:
            backend.return_value.send.side_effect = capture
            e164, message = phone_service.send_phone_otp(raw, request=Context())

        code = ''.join(c for c in sent['body'] if c.isdigit())[: PhoneVerification.otp_length()]
        return e164, code, sent

    def test_a_code_is_sent_to_the_normalised_number(self):
        e164, code, sent = self._send_and_capture_code('0911223344')
        self.assertEqual(e164, '+251911223344')
        self.assertEqual(sent['to'], '+251911223344')
        self.assertEqual(len(code), 6)

    def test_the_plaintext_code_is_never_stored(self):
        _e164, code, _sent = self._send_and_capture_code()
        row = PhoneVerification.objects.get()
        self.assertNotEqual(row.otp_hash, code)
        self.assertNotIn(code, row.otp_hash)

    def test_a_correct_code_verifies_once_and_only_once(self):
        e164, code, _ = self._send_and_capture_code()
        self.assertEqual(phone_service.verify_phone_otp(e164, code, request=Context()), e164)
        # Single-use: the row is gone, so replaying it fails.
        with self.assertRaises(ValidationError):
            phone_service.verify_phone_otp(e164, code, request=Context())

    def test_a_code_verifies_regardless_of_how_the_number_is_typed(self):
        _e164, code, _ = self._send_and_capture_code('+251911223344')
        self.assertEqual(
            phone_service.verify_phone_otp('0911223344', code, request=Context()), '+251911223344'
        )

    def test_a_wrong_code_is_rejected(self):
        e164, code, _ = self._send_and_capture_code()
        wrong = '000000' if code != '000000' else '111111'
        with self.assertRaises(ValidationError):
            phone_service.verify_phone_otp(e164, wrong, request=Context())

    def test_an_expired_code_is_rejected(self):
        e164, code, _ = self._send_and_capture_code()
        PhoneVerification.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            phone_service.verify_phone_otp(e164, code, request=Context())

    def test_attempts_are_limited(self):
        e164, code, _ = self._send_and_capture_code()
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(PhoneVerification.max_attempts()):
            with self.assertRaises(ValidationError):
                phone_service.verify_phone_otp(e164, wrong, request=Context())
        # Exhausted: the row is destroyed, so even the RIGHT code now fails.
        with self.assertRaises(ValidationError):
            phone_service.verify_phone_otp(e164, code, request=Context())

    def test_every_failure_gives_the_same_message(self):
        # "wrong code", "expired", and "no code was ever requested" are useful to
        # an attacker and useless to a user, who asks for another code either way.
        e164, code, _ = self._send_and_capture_code()
        messages = set()

        with self.assertRaises(ValidationError) as wrong:
            phone_service.verify_phone_otp(e164, '000000', request=Context())
        messages.add(str(wrong.exception))

        with self.assertRaises(ValidationError) as never:
            phone_service.verify_phone_otp('+251999888777', code, request=Context())
        messages.add(str(never.exception))

        self.assertEqual(len(messages), 1, f"failure causes are distinguishable: {messages}")

    def test_a_new_code_invalidates_the_previous_one(self):
        e164, first, _ = self._send_and_capture_code()
        PhoneVerification.objects.update(created_at=timezone.now() - timedelta(hours=1))
        _e164, second, _ = self._send_and_capture_code()
        self.assertNotEqual(first, second)
        with self.assertRaises(ValidationError):
            phone_service.verify_phone_otp(e164, first, request=Context())

    def test_the_cooldown_is_not_observable(self):
        # Same reply as a successful send. Otherwise this answers "was a code
        # requested for this number recently".
        _e164, _code, _ = self._send_and_capture_code()
        with patch('accounts.phone_service.get_sms_backend'):
            _e164_2, message = phone_service.send_phone_otp('0911223344', request=Context())
        self.assertEqual(message, phone_service.GENERIC_CODE_SENT)
        self.assertEqual(PhoneVerification.objects.count(), 1, "the cooldown did not hold")

    def test_a_failed_send_leaves_no_cooldown_behind(self):
        # Otherwise our provider breaking locks the user out of retrying.
        with patch('accounts.phone_service.get_sms_backend') as backend:
            backend.return_value.send.side_effect = SmsUnavailable('nope')
            with self.assertRaises(SmsUnavailable):
                phone_service.send_phone_otp('0911223344', request=Context())
        self.assertEqual(PhoneVerification.objects.count(), 0)

    def test_the_message_says_what_the_code_is_for(self):
        # A bare number is indistinguishable from a phishing text.
        _e164, _code, sent = self._send_and_capture_code()
        self.assertIn('EV Charge Hub', sent['body'])

    def test_an_unparseable_number_is_rejected_plainly(self):
        # The caller's typo is not an enumeration signal, and refusing to say
        # "that is not a phone number" would be user-hostile for no gain.
        with self.assertRaises(ValidationError):
            phone_service.send_phone_otp('not a phone', request=Context())


@override_settings(SMS_BACKEND='disabled')
class PhoneFlowWithNoProvider(TestCase):
    def setUp(self):
        clear_rate_limits()

    def test_it_refuses_out_loud_instead_of_dropping_the_code(self):
        with self.assertRaises(SmsUnavailable):
            phone_service.send_phone_otp('0911223344', request=Context())

    def test_the_api_reports_phone_sign_in_as_unavailable(self):
        result = schema.execute('{ authCapabilities { phoneSignIn } }', context=Context())
        self.assertIsNone(result.errors)
        self.assertFalse(result.data['authCapabilities']['phoneSignIn'])


class AuthCapabilitiesTellTheTruth(TestCase):
    def test_google_and_apple_report_unavailable_because_they_are(self):
        # There is no client ID and no verification path. A client that offers
        # these buttons today is offering nothing. W8 makes them real.
        result = schema.execute(
            '{ authCapabilities { googleSignIn appleSignIn passwordSignIn } }', context=Context()
        )
        self.assertIsNone(result.errors)
        self.assertFalse(result.data['authCapabilities']['googleSignIn'])
        self.assertFalse(result.data['authCapabilities']['appleSignIn'])
        self.assertTrue(result.data['authCapabilities']['passwordSignIn'])

    @override_settings(SMS_BACKEND='console')
    def test_phone_availability_follows_real_configuration(self):
        # Not a constant: it moves when the deployment moves.
        result = schema.execute('{ authCapabilities { phoneSignIn } }', context=Context())
        self.assertTrue(result.data['authCapabilities']['phoneSignIn'])


# ── Schema exposure ──────────────────────────────────────────────────────────


class SubjectIsNeverPublic(TestCase):
    def test_no_type_in_the_schema_exposes_an_identity_subject(self):
        # `subject` is the provider's stable cross-app identifier for a person,
        # and for phone it IS the number. Exposing it anywhere leaks who our
        # users are to whoever can read that field.
        for name, gql_type in schema.graphql_schema.type_map.items():
            fields = getattr(gql_type, 'fields', None)
            if not fields or name.startswith('__'):
                continue
            for field_name in fields:
                with self.subTest(type=name, field=field_name):
                    self.assertNotIn(
                        field_name.lower(), {'subject', 'otphash', 'otp_hash', 'tokenversion'}
                    )

    def test_linked_providers_returns_a_label_not_the_subject(self):
        user = User.objects.create_user(username='alice', email='a@example.com', password='123456')
        identity.attach_phone(user=user, phone_e164='+251911223344')

        result = schema.execute(
            '{ me { linkedProviders { provider label verified } } }', context=Context(user=user)
        )
        self.assertIsNone(result.errors)
        rows = result.data['me']['linkedProviders']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['provider'], 'phone')
        self.assertTrue(rows[0]['verified'])
        # Masked, not raw.
        self.assertNotEqual(rows[0]['label'], '+251911223344')
        self.assertTrue(rows[0]['label'].endswith('3344'))

    def test_the_authenticated_user_can_read_their_own_phone_state(self):
        user = User.objects.create_user(username='alice', email='a@example.com', password='123456')
        result = schema.execute('{ me { phoneE164 phoneVerifiedAt } }', context=Context(user=user))
        self.assertIsNone(result.errors)
        # NULL for a legacy account, and clients must handle exactly this.
        self.assertIsNone(result.data['me']['phoneE164'])
        self.assertIsNone(result.data['me']['phoneVerifiedAt'])


class IdentityMutationsRequireASession(TestCase):
    def setUp(self):
        clear_rate_limits()

    def test_link_phone_unlink_and_logout_everywhere_refuse_anonymous_callers(self):
        from django.contrib.auth.models import AnonymousUser

        for mutation in [
            'linkPhone(phone: "0911223344", code: "123456") { success }',
            'unlinkProvider(provider: "google") { success }',
            'logoutEverywhere { success }',
        ]:
            with self.subTest(mutation=mutation):
                result = schema.execute(
                    'mutation { %s }' % mutation, context=Context(user=AnonymousUser())
                )
                self.assertIsNotNone(result.errors)
