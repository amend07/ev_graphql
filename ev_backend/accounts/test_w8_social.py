"""Coverage for social sign-in token verification (accounts/social.py).

The network and crypto are the whole job of this module, so they are mocked at
the two seams it owns: the JWKS key fetch and ``jwt.decode``. What is asserted is
the module's own logic — audience gating, the single generic error for every
verification failure, the issuer check, and the ``(subject, email)`` it returns.
"""

from unittest import mock

import jwt
from django.test import TestCase, override_settings

from accounts import social
from accounts.social import (
    SocialAuthError,
    SocialAuthUnavailable,
    apple_enabled,
    google_enabled,
    verify_apple_identity_token,
    verify_google_id_token,
)


class _Key:
    key = "signing-key"


class EnablementTests(TestCase):
    @override_settings(GOOGLE_OAUTH_CLIENT_IDS=["aud-1"], APPLE_CLIENT_IDS=[])
    def test_google_enabled_when_client_ids_configured(self):
        self.assertTrue(google_enabled())
        self.assertFalse(apple_enabled())

    @override_settings(GOOGLE_OAUTH_CLIENT_IDS=[], APPLE_CLIENT_IDS=None)
    def test_disabled_when_no_client_ids(self):
        self.assertFalse(google_enabled())
        self.assertFalse(apple_enabled())


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=["google-aud"])
class GoogleVerificationTests(TestCase):
    def _mock_decode(self, claims):
        return mock.patch.object(social.jwt, "decode", return_value=claims)

    def _mock_key(self):
        return mock.patch.object(
            social._GOOGLE_JWKS, "get_signing_key_from_jwt", return_value=_Key()
        )

    def test_valid_token_returns_subject_and_email(self):
        claims = {
            "sub": "google-123",
            "email": "person@example.com",
            "iss": "https://accounts.google.com",
        }
        with self._mock_key(), self._mock_decode(claims):
            subject, email = verify_google_id_token("token")
        self.assertEqual(subject, "google-123")
        self.assertEqual(email, "person@example.com")

    def test_missing_email_is_tolerated(self):
        claims = {"sub": "google-123", "iss": "accounts.google.com"}
        with self._mock_key(), self._mock_decode(claims):
            subject, email = verify_google_id_token("token")
        self.assertEqual(subject, "google-123")
        self.assertEqual(email, "")

    def test_bad_signature_is_a_generic_failure(self):
        with self._mock_key(), mock.patch.object(
            social.jwt, "decode", side_effect=jwt.InvalidSignatureError("nope")
        ):
            with self.assertRaises(SocialAuthError):
                verify_google_id_token("token")

    def test_jwks_fetch_error_is_a_generic_failure(self):
        with mock.patch.object(
            social._GOOGLE_JWKS, "get_signing_key_from_jwt",
            side_effect=RuntimeError("network down"),
        ):
            with self.assertRaises(SocialAuthError):
                verify_google_id_token("token")

    def test_unexpected_issuer_is_rejected(self):
        claims = {"sub": "x", "email": "e@x.com", "iss": "https://evil.example"}
        with self._mock_key(), self._mock_decode(claims):
            with self.assertRaises(SocialAuthError):
                verify_google_id_token("token")

    @override_settings(GOOGLE_OAUTH_CLIENT_IDS=[])
    def test_disabled_provider_refuses_loudly(self):
        with self.assertRaises(SocialAuthUnavailable):
            verify_google_id_token("token")


@override_settings(APPLE_CLIENT_IDS=["apple-aud"])
class AppleVerificationTests(TestCase):
    def test_valid_apple_token_returns_subject_and_email(self):
        claims = {
            "sub": "apple-999",
            "email": "a@example.com",
            "iss": "https://appleid.apple.com",
        }
        with mock.patch.object(
            social._APPLE_JWKS, "get_signing_key_from_jwt", return_value=_Key()
        ), mock.patch.object(social.jwt, "decode", return_value=claims):
            subject, email = verify_apple_identity_token("token")
        self.assertEqual(subject, "apple-999")
        self.assertEqual(email, "a@example.com")

    def test_apple_token_without_email_is_normal(self):
        claims = {"sub": "apple-999", "iss": "https://appleid.apple.com"}
        with mock.patch.object(
            social._APPLE_JWKS, "get_signing_key_from_jwt", return_value=_Key()
        ), mock.patch.object(social.jwt, "decode", return_value=claims):
            subject, email = verify_apple_identity_token("token")
        self.assertEqual(email, "")

    @override_settings(APPLE_CLIENT_IDS=[])
    def test_apple_disabled_refuses(self):
        with self.assertRaises(SocialAuthUnavailable):
            verify_apple_identity_token("token")
