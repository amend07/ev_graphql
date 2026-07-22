"""Social sign-in token verification (Sprint W8).

The ONE place that turns a provider's ID token into a verified ``(subject,
email)``. Everything downstream — ``identity.sign_in_with_identity`` — takes it
from there and never touches a token or the network. This is to Google/Apple what
``phone_service`` is to SMS and ``email_service`` is to email: the boundary where
an external proof becomes an internal fact, isolated so the rules that decide
whose account it is stay free of HTTP and crypto.

Google and Apple both issue OpenID Connect ID tokens: RS256 JWTs signed with keys
published at a JWKS endpoint. Verifying one is three checks — fetch the signing
key named by the token's ``kid``, check the RS256 signature, and assert issuer,
audience and expiry. PyJWT's ``PyJWKClient`` does the key fetch and cache;
``jwt.decode`` does the signature and claim checks. No provider SDK, so no new
dependency.

AUDIENCE IS THE CONTROL THAT MATTERS. A validly-signed Google token minted for a
DIFFERENT application is still a genuine Google token — its signature verifies.
Pinning ``aud`` to THIS deployment's own client IDs is what stops such a token
being replayed against us. With no client IDs configured a provider is simply not
enabled: verification refuses loudly (never silently accepts), and
``authCapabilities`` reports it ``false`` so no client offers the button — the
same honesty rule sms.py keeps.
"""

import logging

import jwt
from jwt import PyJWKClient
from django.conf import settings

from ev_backend.errors import APIError

from .models import AuthIdentity

logger = logging.getLogger('accounts.social')

GOOGLE_ISSUERS = {'https://accounts.google.com', 'accounts.google.com'}
GOOGLE_JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'

APPLE_ISSUER = 'https://appleid.apple.com'
APPLE_JWKS_URL = 'https://appleid.apple.com/auth/keys'

# JWKS clients cache keys internally, so build them once at import rather than per
# request; a per-call client would refetch Google's key set on every sign-in.
_GOOGLE_JWKS = PyJWKClient(GOOGLE_JWKS_URL)
_APPLE_JWKS = PyJWKClient(APPLE_JWKS_URL)


class SocialAuthUnavailable(APIError):
    """The provider is not configured, so its token cannot be verified.

    A typed failure, like ``SmsUnavailable``: the client needs to tell "this
    deployment does not support Google" (do not offer the button) apart from
    "your Google token did not verify" (transient, retry). Surfaces as
    ``extensions.code = 'social_auth_unavailable'``.
    """

    code = 'social_auth_unavailable'


class SocialAuthError(APIError):
    """The token did not verify — bad signature, wrong audience, expired, forged.

    One message for every cause, for the same reason the OTP paths give one: the
    distinctions are useful to an attacker and useless to a user, who retries the
    sign-in regardless. The real reason is logged server-side, never returned.
    """

    code = 'social_auth_failed'


def _configured_ids(setting_name):
    """The audience allow-list for a provider, or an empty tuple if disabled."""
    return tuple(getattr(settings, setting_name, ()) or ())


def google_enabled():
    return bool(_configured_ids('GOOGLE_OAUTH_CLIENT_IDS'))


def apple_enabled():
    return bool(_configured_ids('APPLE_CLIENT_IDS'))


def _verify(id_token, *, jwks_client, issuers, audiences, provider):
    """Verify an OIDC ID token and return its validated claims.

    Decodes WITHOUT jwt.decode's own issuer check because Google publishes two
    acceptable issuer strings and that option takes exactly one; the issuer is
    asserted against the set below instead. Audience and expiry are enforced by
    jwt.decode.
    """
    if not audiences:
        raise SocialAuthUnavailable(
            f"{provider.title()} sign-in is not available on this deployment."
        )

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=['RS256'],
            audience=list(audiences),
            options={'require': ['exp', 'iat', 'sub']},
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        # The operator needs the cause to read an incident; the caller does not.
        logger.warning('%s token verification failed: %s', provider, exc)
        raise SocialAuthError("Could not verify that sign-in. Please try again.")
    except Exception as exc:  # JWKS fetch / network — our problem, not the user's
        logger.error('%s JWKS/verification error: %s', provider, exc)
        raise SocialAuthError("Could not verify that sign-in. Please try again.")

    if claims.get('iss') not in issuers:
        logger.warning('%s token has unexpected issuer %r', provider, claims.get('iss'))
        raise SocialAuthError("Could not verify that sign-in. Please try again.")

    return claims


def verify_google_id_token(id_token):
    """Verify a Google ID token. Returns ``(subject, email)``.

    ``subject`` is Google's ``sub`` — the stable, opaque per-user ID that becomes
    ``AuthIdentity.subject``. ``email`` is for record-keeping only and is never
    used to match an existing account (see identity.sign_in_with_identity).
    """
    claims = _verify(
        id_token,
        jwks_client=_GOOGLE_JWKS,
        issuers=GOOGLE_ISSUERS,
        audiences=_configured_ids('GOOGLE_OAUTH_CLIENT_IDS'),
        provider=AuthIdentity.PROVIDER_GOOGLE,
    )
    return claims['sub'], claims.get('email') or ''


def verify_apple_identity_token(id_token):
    """Verify an Apple identity token. Returns ``(subject, email)``.

    Apple returns ``email`` only on the FIRST authorisation (and only if the app
    requested the scope); later tokens omit it. An empty email is therefore normal
    and not an error — the account already exists by then, keyed on ``sub``.
    """
    claims = _verify(
        id_token,
        jwks_client=_APPLE_JWKS,
        issuers={APPLE_ISSUER},
        audiences=_configured_ids('APPLE_CLIENT_IDS'),
        provider=AuthIdentity.PROVIDER_APPLE,
    )
    return claims['sub'], claims.get('email') or ''
