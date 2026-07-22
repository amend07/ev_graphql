"""Phone + PIN and social sign-in (Sprint W8).

The three sign-in options a client now offers — phone+PIN, Google, Apple — and
the one signup path that feeds them, executable. Written under the same rule as
the W7 suite ("a guard you have not seen fail is not a guard"): every assertion
here was watched failing before it was allowed to pass.

The single most important property, tested from several angles: an account
created through the emailed-OTP signup has a PROVEN EMAIL and a CLAIMED-not-proven
PHONE. There is no SMS provider, so the phone cannot be proven, and pretending it
was would be a lie the identity-linking rules later trust. See
docs/IDENTITY_ARCHITECTURE.md §9.2, EmailVerification, and identity.py.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from ev_backend.schema import schema

from . import email_service, identity, services, social
from .models import AuthIdentity, EmailVerification

User = get_user_model()


class Context:
    """A request stand-in. Rate limiting reads META; resolvers read `user`."""

    def __init__(self, user=None, ip='198.51.100.9'):
        self.user = user
        self.META = {'REMOTE_ADDR': ip}


def clear_rate_limits():
    from django.core.cache import cache

    cache.clear()


def issue_signup_code(email, ip='198.51.100.9'):
    """Send a signup code and return the plaintext, by capturing the value the
    delivery step is handed. Mirrors how the W7 suite reads the SMS code: never
    from the database (only the hash is stored), always from delivery.
    """
    captured = {}

    def capture(self, code):  # replaces EmailVerification._send_email
        captured['code'] = code

    with patch.object(EmailVerification, '_send_email', capture):
        email_service.send_signup_otp(email, request=Context(ip=ip))
    return captured['code']


def register(*, phone, email, pin, otp, owner=False, ip='198.51.100.9'):
    return schema.execute(
        '''
        mutation ($phone: String!, $email: String!, $pin: String!, $otp: String!, $owner: Boolean) {
          registerWithPhone(phone: $phone, email: $email, pin: $pin, otp: $otp, isStationOwner: $owner) {
            token
            user { id email phoneE164 phoneVerifiedAt role ownerStatus linkedProviders { provider verified } }
          }
        }
        ''',
        variables={'phone': phone, 'email': email, 'pin': pin, 'otp': otp, 'owner': owner},
        context=Context(ip=ip),
    )


# ── Signup: emailed OTP, phone + PIN ─────────────────────────────────────────


class PhonePinSignup(TestCase):
    def setUp(self):
        clear_rate_limits()

    def test_a_full_signup_creates_an_account_and_returns_a_session(self):
        code = issue_signup_code('driver@example.com')
        result = register(
            phone='0911223344', email='driver@example.com', pin='481902', otp=code,
        )
        self.assertIsNone(result.errors)
        data = result.data['registerWithPhone']
        self.assertTrue(data['token'])
        self.assertEqual(data['user']['email'], 'driver@example.com')
        self.assertEqual(data['user']['phoneE164'], '+251911223344')  # normalised
        self.assertEqual(data['user']['role'], 'user')

    def test_the_phone_is_canonical_but_NOT_marked_verified(self):
        # The property this whole sprint turns on: an emailed code proves the
        # mailbox, not the handset. A verified phone here would be a lie.
        code = issue_signup_code('claim@example.com')
        register(phone='0911223345', email='claim@example.com', pin='481902', otp=code)

        user = User.objects.get(email='claim@example.com')
        self.assertEqual(user.phone_e164, '+251911223345')      # canonical
        self.assertIsNone(user.phone_verified_at)               # but not proven
        self.assertFalse(user.has_verified_phone())
        # ...and no verified `phone` identity was written.
        self.assertFalse(
            AuthIdentity.objects.filter(user=user, provider=AuthIdentity.PROVIDER_PHONE).exists()
        )

    def test_the_pin_is_stored_as_a_password_identity_hashed(self):
        code = issue_signup_code('pin@example.com')
        register(phone='0911223346', email='pin@example.com', pin='481902', otp=code)

        user = User.objects.get(email='pin@example.com')
        self.assertTrue(user.check_password('481902'))          # hashed, verifiable
        self.assertNotIn('481902', user.password)               # never plaintext
        self.assertTrue(
            AuthIdentity.objects.filter(user=user, provider=AuthIdentity.PROVIDER_PASSWORD).exists()
        )

    def test_a_wrong_code_is_refused_and_no_account_appears(self):
        issue_signup_code('nope@example.com')
        result = register(phone='0911223347', email='nope@example.com', pin='481902', otp='000000')
        self.assertIsNotNone(result.errors)
        self.assertFalse(User.objects.filter(email='nope@example.com').exists())

    def test_a_predictable_failure_does_not_burn_the_single_use_code(self):
        # A weak PIN is caught BEFORE the code is spent, so the caller can retry
        # with the SAME code — the rule reset_pin keeps for a bad new PIN.
        code = issue_signup_code('retry@example.com')
        bad = register(phone='0911223348', email='retry@example.com', pin='12', otp=code)
        self.assertIsNotNone(bad.errors)

        good = register(phone='0911223348', email='retry@example.com', pin='481902', otp=code)
        self.assertIsNone(good.errors)
        self.assertTrue(good.data['registerWithPhone']['token'])

    def test_a_duplicate_phone_is_refused(self):
        code = issue_signup_code('first@example.com')
        register(phone='0911223349', email='first@example.com', pin='481902', otp=code)

        code2 = issue_signup_code('second@example.com')
        dup = register(phone='+251911223349', email='second@example.com', pin='481902', otp=code2)
        self.assertIsNotNone(dup.errors)
        self.assertIn('phone', dup.errors[0].message.lower())

    def test_a_duplicate_email_is_refused(self):
        User.objects.create_user(username='u_dup', email='taken@example.com', password='481902')
        code = issue_signup_code('taken@example.com')
        dup = register(phone='0911777788', email='taken@example.com', pin='481902', otp=code)
        self.assertIsNotNone(dup.errors)

    def test_a_pin_that_is_not_six_digits_is_refused(self):
        code = issue_signup_code('weak@example.com')
        result = register(phone='0911223350', email='weak@example.com', pin='abcd', otp=code)
        self.assertIsNotNone(result.errors)

    def test_registering_as_a_station_owner_starts_pending(self):
        code = issue_signup_code('owner@example.com')
        result = register(
            phone='0911223351', email='owner@example.com', pin='481902', otp=code, owner=True,
        )
        self.assertIsNone(result.errors)
        user = User.objects.get(email='owner@example.com')
        self.assertEqual(user.role, 'station_owner')
        self.assertEqual(user.owner_status, User.OWNER_PENDING)

    def test_the_code_is_actually_emailed_and_says_what_it_is_for(self):
        from django.core import mail

        with override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            email_service.send_signup_otp('mailed@example.com', request=Context())
            self.assertEqual(len(mail.outbox), 1)
            self.assertIn('mailed@example.com', mail.outbox[0].to)
            self.assertIn('EV Charge Hub', mail.outbox[0].body)


# ── Phone + PIN sign-in ──────────────────────────────────────────────────────


class PhonePinSignIn(TestCase):
    def setUp(self):
        clear_rate_limits()
        code = issue_signup_code('login@example.com')
        register(phone='0911223360', email='login@example.com', pin='481902', otp=code)
        clear_rate_limits()

    def _sign_in(self, phone, pin, ip='203.0.113.5'):
        return schema.execute(
            '''
            mutation ($phone: String!, $pin: String!) {
              signInWithPhonePin(phone: $phone, pin: $pin) { token user { email } }
            }
            ''',
            variables={'phone': phone, 'pin': pin},
            context=Context(ip=ip),
        )

    def test_correct_phone_and_pin_returns_a_session(self):
        result = self._sign_in('0911223360', '481902')
        self.assertIsNone(result.errors)
        self.assertTrue(result.data['signInWithPhonePin']['token'])
        self.assertEqual(result.data['signInWithPhonePin']['user']['email'], 'login@example.com')

    def test_any_format_of_the_registered_number_signs_in(self):
        # Registered as 0911...; signing in with the +251... form must resolve to
        # the same account — the reason phone.py exists.
        result = self._sign_in('+251911223360', '481902')
        self.assertIsNone(result.errors)
        self.assertTrue(result.data['signInWithPhonePin']['token'])

    def test_a_wrong_pin_is_refused_generically(self):
        result = self._sign_in('0911223360', '000000')
        self.assertIsNotNone(result.errors)
        self.assertIsNone(result.data['signInWithPhonePin'])

    def test_an_unknown_number_gives_the_same_answer_as_a_wrong_pin(self):
        wrong_pin = self._sign_in('0911223360', '000000', ip='203.0.113.6')
        unknown = self._sign_in('0912000000', '481902', ip='203.0.113.7')
        self.assertIsNotNone(wrong_pin.errors)
        self.assertIsNotNone(unknown.errors)
        # Indistinguishable: same message for "wrong PIN" and "no such number".
        self.assertEqual(wrong_pin.errors[0].message, unknown.errors[0].message)


# ── setMyPhone: the phone a social signup collects, UNVERIFIED ────────────────


class SetMyPhoneCollectsUnverified(TestCase):
    def setUp(self):
        clear_rate_limits()
        self.user = User.objects.create_user(
            username='google_abc', email='social@example.com', password=None,
        )

    def _set_phone(self, user, phone):
        return schema.execute(
            'mutation ($p: String!) { setMyPhone(phone: $p) { success user { phoneE164 phoneVerifiedAt } } }',
            variables={'p': phone},
            context=Context(user=user),
        )

    def test_it_stores_the_number_as_canonical_but_unproven(self):
        result = self._set_phone(self.user, '0911445566')
        self.assertIsNone(result.errors)
        self.assertEqual(result.data['setMyPhone']['user']['phoneE164'], '+251911445566')
        self.assertIsNone(result.data['setMyPhone']['user']['phoneVerifiedAt'])

        self.user.refresh_from_db()
        self.assertIsNone(self.user.phone_verified_at)
        # No `phone` identity — an identity row would assert a proof that never
        # happened. Contrast linkPhone, which spends an OTP and DOES write one.
        self.assertFalse(
            AuthIdentity.objects.filter(user=self.user, provider=AuthIdentity.PROVIDER_PHONE).exists()
        )

    def test_it_refuses_a_number_another_account_already_holds(self):
        other = User.objects.create_user(username='other', email='o@example.com', password='481902')
        identity.set_unverified_phone(user=other, phone_e164='0911445577')

        result = self._set_phone(self.user, '+251911445577')
        self.assertIsNotNone(result.errors)

    def test_it_requires_a_session(self):
        result = self._set_phone(Context().user, '0911445588')  # user=None
        self.assertIsNotNone(result.errors)


# ── Google / Apple sign-in ───────────────────────────────────────────────────


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=['web.apps.googleusercontent.com'])
class GoogleSignIn(TestCase):
    def setUp(self):
        clear_rate_limits()

    def _sign_in(self, id_token='tok'):
        return schema.execute(
            '''
            mutation ($t: String!) {
              signInWithGoogle(idToken: $t) { token created user { email } }
            }
            ''',
            variables={'t': id_token},
            context=Context(),
        )

    def test_a_verified_token_creates_an_account_the_first_time(self):
        with patch.object(social, 'verify_google_id_token', return_value=('g-sub-1', 'gmail@example.com')):
            result = self._sign_in()
        self.assertIsNone(result.errors)
        self.assertTrue(result.data['signInWithGoogle']['created'])
        self.assertTrue(result.data['signInWithGoogle']['token'])
        self.assertTrue(
            AuthIdentity.objects.filter(provider=AuthIdentity.PROVIDER_GOOGLE, subject='g-sub-1').exists()
        )

    def test_the_second_sign_in_returns_the_same_account(self):
        with patch.object(social, 'verify_google_id_token', return_value=('g-sub-2', 'again@example.com')):
            first = self._sign_in()
            second = self._sign_in()
        self.assertTrue(first.data['signInWithGoogle']['created'])
        self.assertFalse(second.data['signInWithGoogle']['created'])
        self.assertEqual(User.objects.filter(identities__subject='g-sub-2').count(), 1)

    def test_email_is_never_used_to_hijack_an_existing_account(self):
        # The anti-takeover rule: a pre-existing account with the same email must
        # NOT be adopted by a Google sign-in. Our stored emails were never proven.
        victim = User.objects.create_user(
            username='victim', email='shared@example.com', password='481902',
        )
        with patch.object(social, 'verify_google_id_token', return_value=('g-sub-3', 'shared@example.com')):
            result = self._sign_in()
        self.assertIsNone(result.errors)
        new_user_id = User.objects.get(identities__subject='g-sub-3').id
        self.assertNotEqual(new_user_id, victim.id)  # a NEW account, not the victim's

    def test_a_new_google_user_has_no_phone_until_they_set_one(self):
        with patch.object(social, 'verify_google_id_token', return_value=('g-sub-4', 'n@example.com')):
            self._sign_in()
        user = User.objects.get(identities__subject='g-sub-4')
        self.assertIsNone(user.phone_e164)  # the client routes them to setMyPhone


class AppleSignIn(TestCase):
    def setUp(self):
        clear_rate_limits()

    @override_settings(APPLE_CLIENT_IDS=['com.evchargehub.app'])
    def test_apple_signup_works_even_when_the_token_carries_no_email(self):
        # Apple omits the email on all but the first authorisation; an empty email
        # is normal, not an error.
        with patch.object(social, 'verify_apple_identity_token', return_value=('a-sub-1', '')):
            result = schema.execute(
                'mutation ($t: String!) { signInWithApple(identityToken: $t) { token created } }',
                variables={'t': 'tok'},
                context=Context(),
            )
        self.assertIsNone(result.errors)
        self.assertTrue(result.data['signInWithApple']['created'])
        self.assertTrue(result.data['signInWithApple']['token'])


# ── Capabilities honestly reflect configuration ──────────────────────────────


class AuthCapabilitiesReflectConfiguration(TestCase):
    def _caps(self):
        result = schema.execute(
            '{ authCapabilities { phoneSignIn googleSignIn appleSignIn passwordSignIn } }',
            context=Context(),
        )
        self.assertIsNone(result.errors)
        return result.data['authCapabilities']

    def test_phone_and_password_are_always_available(self):
        caps = self._caps()
        self.assertTrue(caps['phoneSignIn'])
        self.assertTrue(caps['passwordSignIn'])

    def test_google_and_apple_are_false_without_client_ids(self):
        caps = self._caps()
        self.assertFalse(caps['googleSignIn'])
        self.assertFalse(caps['appleSignIn'])

    @override_settings(GOOGLE_OAUTH_CLIENT_IDS=['x'], APPLE_CLIENT_IDS=['y'])
    def test_google_and_apple_turn_on_when_configured(self):
        caps = self._caps()
        self.assertTrue(caps['googleSignIn'])
        self.assertTrue(caps['appleSignIn'])

    def test_an_unconfigured_provider_refuses_verification(self):
        # With no client IDs, social verification must refuse loudly rather than
        # accept anything — the honesty rule from sms.py, applied to tokens.
        with self.assertRaises(social.SocialAuthUnavailable):
            social.verify_google_id_token('anything')
