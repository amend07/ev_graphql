"""Automated tests for the hardened authentication subsystem (Sprint 3).

Covers registration, login, PIN validation, PIN reset, PIN change, OTP
generation/expiration/verification/attempt-limits, rate limiting, JWT
configuration/expiration, invalid credentials, backward-compatible aliases,
and the guarantee that credentials/OTPs are hashed and never exposed.
"""

import secrets
from datetime import timedelta
from unittest import mock

from django.test import TestCase, RequestFactory, override_settings
from django.contrib.auth import get_user_model, authenticate
from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone
from graphene.test import Client

from ev_backend.schema import schema
from accounts import ratelimit, services
from accounts.models import PasswordResetOTP
from accounts.validators import validate_pin, SixDigitPINValidator, PIN_LENGTH

User = get_user_model()

VALID_PIN = "123456"
INVALID_PINS = [
    "12345", "1234567", "12a456", "abcdef", "",
    "12 456", " 123456", "123456 ", "-12345", "12.456",
]


def make_user(username="user", email=None, pin=VALID_PIN, **extra):
    return User.objects.create_user(
        username=username, email=email or f"{username}@example.com",
        password=pin, role=extra.pop("role", "user"), **extra
    )


class BaseAuthTest(TestCase):
    def setUp(self):
        cache.clear()  # isolate rate-limit state between tests
        self.client = Client(schema)
        self.factory = RequestFactory()

    def ctx(self, user=None):
        request = self.factory.post("/graphql/")
        request.user = user or AnonymousUser()
        return request


# ── PIN validation ───────────────────────────────────────────────────────────

class PinValidatorTests(TestCase):
    def test_valid_pin_passes(self):
        validate_pin(VALID_PIN)
        SixDigitPINValidator().validate(VALID_PIN)

    def test_invalid_pins_rejected(self):
        for bad in INVALID_PINS:
            with self.subTest(pin=bad):
                with self.assertRaises(ValidationError):
                    validate_pin(bad)

    def test_none_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pin(None)

    def test_constants(self):
        self.assertEqual(PIN_LENGTH, 6)
        self.assertIn("6", str(SixDigitPINValidator().get_help_text()))


# ── Registration ─────────────────────────────────────────────────────────────

class RegistrationTests(BaseAuthTest):
    QUERY = """
        mutation($u:String!,$e:String!,$p:String!){
          createUser(username:$u, email:$e, pin:$p){ success userId }
        }
    """

    def test_valid_pin_creates_hashed_credential(self):
        res = self.client.execute(self.QUERY, variables={"u": "alice", "e": "a@x.com", "p": VALID_PIN})
        self.assertIsNone(res.get("errors"))
        user = User.objects.get(username="alice")
        self.assertNotEqual(user.password, VALID_PIN)
        self.assertNotIn(VALID_PIN, user.password)
        self.assertTrue(user.check_password(VALID_PIN))

    def test_invalid_pins_rejected(self):
        for i, bad in enumerate(INVALID_PINS):
            with self.subTest(pin=bad):
                res = self.client.execute(self.QUERY, variables={"u": f"u{i}", "e": f"u{i}@x.com", "p": bad})
                self.assertIsNotNone(res.get("errors"))
                self.assertFalse(User.objects.filter(username=f"u{i}").exists())

    def test_duplicate_username_and_email_rejected(self):
        make_user("bob", "bob@x.com")
        dup_u = self.client.execute(self.QUERY, variables={"u": "bob", "e": "new@x.com", "p": VALID_PIN})
        dup_e = self.client.execute(self.QUERY, variables={"u": "new", "e": "bob@x.com", "p": VALID_PIN})
        self.assertIsNotNone(dup_u.get("errors"))
        self.assertIsNotNone(dup_e.get("errors"))

    def test_legacy_password_argument_still_works(self):
        q = """mutation($u:String!,$e:String!,$p:String!){
                 createUser(username:$u, email:$e, password:$p){ success } }"""
        res = self.client.execute(q, variables={"u": "carol", "e": "c@x.com", "p": VALID_PIN})
        self.assertIsNone(res.get("errors"))
        self.assertTrue(User.objects.get(username="carol").check_password(VALID_PIN))


# ── Login / invalid credentials ──────────────────────────────────────────────

class LoginTests(BaseAuthTest):
    def setUp(self):
        super().setUp()
        self.user = make_user("dan", "dan@x.com")

    def test_authenticate_correct_pin(self):
        self.assertEqual(authenticate(username="dan", password=VALID_PIN), self.user)

    def test_authenticate_wrong_pin(self):
        self.assertIsNone(authenticate(username="dan", password="000000"))

    def test_token_auth_accepts_pin_and_password_args(self):
        res = self.client.execute("{ __type(name:\"Mutation\"){ fields{ name args{ name } } } }")
        fields = {f["name"]: f for f in res["data"]["__type"]["fields"]}
        args = {a["name"] for a in fields["tokenAuth"]["args"]}
        self.assertIn("pin", args)
        self.assertIn("password", args)  # legacy alias preserved


# ── PIN change ───────────────────────────────────────────────────────────────

class ChangePinTests(BaseAuthTest):
    def setUp(self):
        super().setUp()
        self.user = make_user("erin", "erin@x.com")

    def _change(self, current, new, field="changePin", a="currentPin", b="newPin", user=None):
        q = f"mutation($c:String!,$n:String!){{ {field}({a}:$c, {b}:$n){{ success message }} }}"
        return self.client.execute(q, variables={"c": current, "n": new}, context=self.ctx(user or self.user))

    def test_valid_change_rehashes(self):
        res = self._change(VALID_PIN, "654321")
        self.assertTrue(res["data"]["changePin"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("654321"))
        self.assertFalse(self.user.check_password(VALID_PIN))

    def test_wrong_current_rejected(self):
        res = self._change("000000", "654321")
        self.assertFalse(res["data"]["changePin"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))

    def test_invalid_new_pin_rejected(self):
        for bad in ["12345", "abcdef", ""]:
            with self.subTest(pin=bad):
                res = self._change(VALID_PIN, bad)
                self.assertFalse(res["data"]["changePin"]["success"])

    def test_requires_authentication(self):
        res = self._change(VALID_PIN, "654321", user=AnonymousUser())
        self.assertIsNotNone(res.get("errors"))

    def test_legacy_change_password_alias(self):
        res = self._change(VALID_PIN, "654321", field="changePassword",
                           a="currentPassword", b="newPassword")
        self.assertTrue(res["data"]["changePassword"]["success"])


# ── OTP model: generation / expiration / verification / attempts ─────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OtpModelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("fay", "fay@x.com")

    def test_generation_is_hashed_random_and_emailed(self):
        # Make only the OTP-code digits deterministic. `secrets` is a shared
        # module, so patching its `choice` also intercepts Django's
        # make_password() salt generation — delegate any non-digit alphabet
        # (i.e. the salt) back to the real RNG so it isn't starved.
        real_choice = secrets.choice
        digits = iter("246810")

        def fake_choice(alphabet):
            if alphabet == "0123456789":
                return next(digits)
            return real_choice(alphabet)

        with mock.patch("accounts.models.secrets.choice", side_effect=fake_choice):
            otp = PasswordResetOTP.generate_for_user(self.user)
        # Stored value is a hash, not the code; the code verifies against it.
        self.assertNotEqual(otp.otp_hash, "246810")
        self.assertTrue(check_password("246810", otp.otp_hash))
        # No recoverable plaintext field exists on the model.
        self.assertFalse(hasattr(otp, "otp"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("246810", "".join(f.subject for f in mail.outbox))

    def test_expired_otp_rejected_and_deleted(self):
        otp = PasswordResetOTP.objects.create(
            user=self.user, otp_hash=make_password("654321"),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(otp.verify("654321"), "expired")
        self.assertFalse(PasswordResetOTP.objects.filter(pk=otp.pk).exists())

    def test_correct_otp_verifies_once(self):
        otp = PasswordResetOTP.objects.create(
            user=self.user, otp_hash=make_password("654321"),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertEqual(otp.verify("654321"), "ok")
        self.assertFalse(PasswordResetOTP.objects.filter(pk=otp.pk).exists())

    @override_settings(OTP_MAX_ATTEMPTS=3)
    def test_attempt_limit_locks_out(self):
        otp = PasswordResetOTP.objects.create(
            user=self.user, otp_hash=make_password("654321"),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertEqual(otp.verify("000000"), "invalid")
        self.assertEqual(otp.verify("111111"), "invalid")
        self.assertEqual(otp.verify("222222"), "locked")
        self.assertFalse(PasswordResetOTP.objects.filter(pk=otp.pk).exists())

    @override_settings(OTP_REQUEST_COOLDOWN_SECONDS=60)
    def test_cooldown_between_requests(self):
        PasswordResetOTP.objects.create(
            user=self.user, otp_hash=make_password("654321"),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        allowed, wait = PasswordResetOTP.can_request(self.user)
        self.assertFalse(allowed)
        self.assertGreater(wait, 0)


# ── PIN reset flow (through the service/mutation) ────────────────────────────

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ResetPinTests(BaseAuthTest):
    def setUp(self):
        super().setUp()
        self.user = make_user("gil", "gil@x.com")

    def _otp(self, code="654321", minutes=10):
        PasswordResetOTP.objects.filter(user=self.user).delete()
        return PasswordResetOTP.objects.create(
            user=self.user, otp_hash=make_password(code),
            expires_at=timezone.now() + timedelta(minutes=minutes),
        )

    def _reset(self, otp, new_pin, email=None):
        q = """mutation($e:String!,$o:String!,$p:String!){
                 resetPinWithOtp(email:$e, otp:$o, newPin:$p){ success message } }"""
        return self.client.execute(q, variables={"e": email or self.user.email, "o": otp, "p": new_pin},
                                   context=self.ctx())

    def test_valid_reset(self):
        self._otp("654321")
        res = self._reset("654321", "112233")
        self.assertTrue(res["data"]["resetPinWithOtp"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("112233"))
        self.assertFalse(PasswordResetOTP.objects.filter(user=self.user).exists())

    def test_invalid_new_pin_keeps_otp_and_old_pin(self):
        self._otp("654321")
        res = self._reset("654321", "12345")
        self.assertFalse(res["data"]["resetPinWithOtp"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))

    def test_wrong_otp_rejected_generically(self):
        self._otp("654321")
        res = self._reset("000000", "112233")
        self.assertFalse(res["data"]["resetPinWithOtp"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))

    def test_unknown_email_is_generic(self):
        res = self._reset("654321", "112233", email="nobody@x.com")
        self.assertFalse(res["data"]["resetPinWithOtp"]["success"])

    def test_send_otp_is_generic_for_unknown_email(self):
        q = """mutation($e:String!){ sendPinResetOtp(email:$e){ success message } }"""
        known = self.client.execute(q, variables={"e": self.user.email}, context=self.ctx())
        unknown = self.client.execute(q, variables={"e": "nobody@x.com"}, context=self.ctx())
        # Identical generic response regardless of existence (anti-enumeration).
        self.assertEqual(known["data"]["sendPinResetOtp"]["message"],
                         unknown["data"]["sendPinResetOtp"]["message"])

    def test_legacy_reset_password_alias(self):
        self._otp("654321")
        q = """mutation($e:String!,$o:String!,$p:String!){
                 resetPasswordWithOtp(email:$e, otp:$o, newPassword:$p){ success } }"""
        res = self.client.execute(q, variables={"e": self.user.email, "o": "654321", "p": "112233"},
                                  context=self.ctx())
        self.assertTrue(res["data"]["resetPasswordWithOtp"]["success"])


# ── Rate limiting ────────────────────────────────────────────────────────────

class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(AUTH_RATELIMIT={"LOGIN": {"limit": 3, "window": 60, "lockout": 60}})
    def test_enforce_locks_after_limit(self):
        for _ in range(3):
            ratelimit.enforce("LOGIN", "1.2.3.4", "ip")
        with self.assertRaises(ratelimit.RateLimitExceeded):
            ratelimit.enforce("LOGIN", "1.2.3.4", "ip")

    @override_settings(AUTH_RATELIMIT={"LOGIN": {"limit": 3, "window": 60, "lockout": 60}})
    def test_reset_clears_counter(self):
        for _ in range(3):
            ratelimit.enforce("LOGIN", "9.9.9.9", "ip")
        ratelimit.reset("LOGIN", "9.9.9.9", "ip")
        ratelimit.enforce("LOGIN", "9.9.9.9", "ip")  # should not raise

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        AUTH_RATELIMIT={"OTP_REQUEST": {"limit": 2, "window": 60, "lockout": 60}},
    )
    def test_otp_request_rate_limited(self):
        make_user("hal", "hal@x.com")
        client = Client(schema)
        factory = RequestFactory()
        ctx = factory.post("/graphql/"); ctx.user = AnonymousUser()
        q = """mutation($e:String!){ sendPinResetOtp(email:$e){ success message } }"""
        for _ in range(2):
            client.execute(q, variables={"e": "hal@x.com"}, context=ctx)
        blocked = client.execute(q, variables={"e": "hal@x.com"}, context=ctx)
        self.assertFalse(blocked["data"]["sendPinResetOtp"]["success"])

    def test_get_client_ip_prefers_forwarded_for(self):
        req = RequestFactory().post("/graphql/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")
        self.assertEqual(ratelimit.get_client_ip(req), "203.0.113.7")


# ── JWT configuration & expiration ───────────────────────────────────────────

class JwtTests(TestCase):
    def test_secure_jwt_settings(self):
        from django.conf import settings
        cfg = settings.GRAPHQL_JWT
        self.assertEqual(cfg["JWT_ALGORITHM"], "HS256")
        self.assertTrue(cfg["JWT_VERIFY_EXPIRATION"])
        self.assertTrue(cfg["JWT_ALLOW_REFRESH"])
        self.assertIn("JWT_EXPIRATION_DELTA", cfg)
        self.assertIn("JWT_REFRESH_EXPIRATION_DELTA", cfg)
        self.assertEqual(cfg["JWT_AUTH_HEADER_PREFIX"], "JWT")  # Flutter compatibility

    def test_valid_token_round_trip(self):
        from graphql_jwt.shortcuts import get_token
        from graphql_jwt.utils import get_payload
        user = make_user("iris", "iris@x.com")
        payload = get_payload(get_token(user))
        self.assertEqual(payload[user.USERNAME_FIELD], user.username)

    def test_expired_token_is_rejected(self):
        import jwt as pyjwt
        from graphql_jwt.settings import jwt_settings
        from graphql_jwt.utils import get_payload
        user = make_user("jack", "jack@x.com")
        expired = pyjwt.encode(
            {user.USERNAME_FIELD: user.username,
             "exp": timezone.now() - timedelta(seconds=5)},
            jwt_settings.JWT_SECRET_KEY, algorithm=jwt_settings.JWT_ALGORITHM,
        )
        with self.assertRaises(Exception):
            get_payload(expired)


# ── Sensitive-data exposure ──────────────────────────────────────────────────

class ExposureTests(TestCase):
    def test_user_type_hides_credential(self):
        res = Client(schema).execute('{ __type(name:"UserType"){ fields{ name } } }')
        names = {f["name"] for f in res["data"]["__type"]["fields"]}
        self.assertNotIn("password", names)
        self.assertNotIn("pin", names)

    def test_otp_type_hides_code_and_hash(self):
        res = Client(schema).execute('{ __type(name:"OTPType"){ fields{ name } } }')
        names = {f["name"] for f in res["data"]["__type"]["fields"]}
        self.assertNotIn("otp", names)
        self.assertNotIn("otpHash", names)
