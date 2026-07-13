"""Tests for the 6-digit numeric PIN policy.

Covers the validator, and every credential path (registration, login, PIN
change, PIN reset) for both valid and invalid PINs, plus the guarantee that the
PIN is hashed and never stored or exposed in plaintext.
"""

from datetime import timedelta

from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model, authenticate
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.utils import timezone
from graphene.test import Client

from ev_backend.schema import schema
from accounts.models import PasswordResetOTP
from accounts.validators import validate_pin, SixDigitPINValidator, PIN_LENGTH

User = get_user_model()

VALID_PIN = "123456"
# Wrong length, non-numeric, empty, and embedded whitespace/sign/separators.
INVALID_PINS = [
    "12345",     # too short
    "1234567",   # too long
    "12a456",    # letter
    "abcdef",    # all letters
    "",          # empty
    "12 456",    # embedded space
    " 123456",   # leading space
    "123456 ",   # trailing space
    "-12345",    # sign
    "12.456",    # separator
]


class PinValidatorTests(TestCase):
    def test_valid_pin_passes(self):
        validate_pin(VALID_PIN)                 # must not raise
        SixDigitPINValidator().validate(VALID_PIN)

    def test_invalid_pins_rejected(self):
        for bad in INVALID_PINS:
            with self.subTest(pin=bad):
                with self.assertRaises(ValidationError):
                    validate_pin(bad)
                with self.assertRaises(ValidationError):
                    SixDigitPINValidator().validate(bad)

    def test_none_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pin(None)

    def test_policy_constants(self):
        self.assertEqual(PIN_LENGTH, 6)
        self.assertIn("6", str(SixDigitPINValidator().get_help_text()))


class RegistrationPinTests(TestCase):
    def setUp(self):
        self.client = Client(schema)

    def _register(self, pin, username="alice", email="alice@example.com"):
        query = """
            mutation($u:String!, $e:String!, $p:String!){
              createUser(username:$u, email:$e, pin:$p){ success userId }
            }
        """
        return self.client.execute(
            query, variables={"u": username, "e": email, "p": pin}
        )

    def test_valid_pin_creates_hashed_credential(self):
        res = self._register(VALID_PIN)
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["createUser"]["success"])

        user = User.objects.get(username="alice")
        # Never stored in plaintext, and verifiable through the hasher.
        self.assertNotEqual(user.password, VALID_PIN)
        self.assertNotIn(VALID_PIN, user.password)
        self.assertTrue(user.check_password(VALID_PIN))

    def test_invalid_pins_rejected_and_no_user_created(self):
        for i, bad in enumerate(INVALID_PINS):
            with self.subTest(pin=bad):
                res = self._register(bad, username=f"u{i}", email=f"u{i}@example.com")
                self.assertIsNotNone(res.get("errors"))
                self.assertFalse(User.objects.filter(username=f"u{i}").exists())


class LoginPinTests(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.user = User.objects.create_user(
            username="bob", email="bob@example.com", password=VALID_PIN, role="user"
        )

    def test_authenticate_with_correct_pin(self):
        self.assertEqual(authenticate(username="bob", password=VALID_PIN), self.user)

    def test_authenticate_with_wrong_pin_fails(self):
        self.assertIsNone(authenticate(username="bob", password="000000"))

    def test_token_auth_exposes_pin_argument_not_password(self):
        # The login credential must be surfaced as `pin`, not `password`.
        introspection = """
            { __type(name:"Mutation"){ fields{ name args{ name } } } }
        """
        res = self.client.execute(introspection)
        fields = {f["name"]: f for f in res["data"]["__type"]["fields"]}
        self.assertIn("tokenAuth", fields)
        arg_names = {a["name"] for a in fields["tokenAuth"]["args"]}
        self.assertIn("pin", arg_names)
        self.assertNotIn("password", arg_names)


class ChangePinTests(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.user = User.objects.create_user(
            username="carol", email="carol@example.com", password=VALID_PIN, role="user"
        )
        self.factory = RequestFactory()

    def _ctx(self, user):
        request = self.factory.post("/graphql/")
        request.user = user
        return request

    def _change(self, current, new, user=None):
        query = """
            mutation($c:String!, $n:String!){
              changePin(currentPin:$c, newPin:$n){ success message }
            }
        """
        return self.client.execute(
            query, variables={"c": current, "n": new}, context=self._ctx(user or self.user)
        )

    def test_valid_change_rehashes_and_invalidates_old_pin(self):
        res = self._change(VALID_PIN, "654321")
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["changePin"]["success"])

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("654321"))
        self.assertFalse(self.user.check_password(VALID_PIN))

    def test_wrong_current_pin_rejected(self):
        res = self._change("000000", "654321")
        self.assertFalse(res["data"]["changePin"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))

    def test_invalid_new_pin_rejected(self):
        for bad in ["12345", "abcdef", "1234567", ""]:
            with self.subTest(pin=bad):
                res = self._change(VALID_PIN, bad)
                self.assertFalse(res["data"]["changePin"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))

    def test_requires_authentication(self):
        res = self.client.execute(
            "mutation($c:String!,$n:String!){ changePin(currentPin:$c,newPin:$n){ success } }",
            variables={"c": VALID_PIN, "n": "654321"},
            context=self._ctx(AnonymousUser()),
        )
        self.assertIsNotNone(res.get("errors"))


class ResetPinTests(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.user = User.objects.create_user(
            username="dan", email="dan@example.com", password=VALID_PIN, role="user"
        )

    def _otp(self, code="654321", minutes=10):
        PasswordResetOTP.objects.filter(user=self.user).delete()
        return PasswordResetOTP.objects.create(
            user=self.user, otp=code, expires_at=timezone.now() + timedelta(minutes=minutes)
        )

    def _reset(self, otp, new_pin, email=None):
        query = """
            mutation($e:String!, $o:String!, $p:String!){
              resetPinWithOtp(email:$e, otp:$o, newPin:$p){ success message }
            }
        """
        return self.client.execute(
            query, variables={"e": email or self.user.email, "o": otp, "p": new_pin}
        )

    def test_valid_reset_rehashes_and_consumes_otp(self):
        self._otp("654321")
        res = self._reset("654321", "112233")
        self.assertIsNone(res.get("errors"))
        self.assertTrue(res["data"]["resetPinWithOtp"]["success"])

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("112233"))
        self.assertFalse(PasswordResetOTP.objects.filter(user=self.user).exists())

    def test_invalid_new_pin_rejected(self):
        self._otp("654321")
        res = self._reset("654321", "12345")
        self.assertFalse(res["data"]["resetPinWithOtp"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))

    def test_wrong_otp_rejected(self):
        self._otp("654321")
        res = self._reset("000000", "112233")
        self.assertFalse(res["data"]["resetPinWithOtp"]["success"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(VALID_PIN))


class PinExposureTests(TestCase):
    def test_user_type_never_exposes_credential(self):
        # The GraphQL User type must not surface the password/PIN hash field.
        res = Client(schema).execute(
            '{ __type(name:"UserType"){ fields{ name } } }'
        )
        field_names = {f["name"] for f in res["data"]["__type"]["fields"]}
        self.assertNotIn("password", field_names)
        self.assertNotIn("pin", field_names)
