"""Identity service (Sprint W7).

The linking rules from docs/IDENTITY_ARCHITECTURE.md §3, in one place, as the
only way an identity is ever attached to a user. Every rule here exists because
its absence is an account takeover — this module is the security core of the
sprint, not plumbing.

Pure domain logic: no GraphQL, no provider SDKs, no HTTP. It takes an already-
verified `(provider, subject)` and decides what that means. Verifying the token
is the caller's job; deciding whose account it is, is this module's.
"""

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from ev_backend.errors import Conflict, NotFound, ValidationError

from .models import AuthIdentity, User


def find_by_identity(provider, subject):
    """The user this identity belongs to, or None."""
    identity = (
        AuthIdentity.objects.filter(provider=provider, subject=subject)
        .select_related('user')
        .first()
    )
    return identity.user if identity else None


def link_identity(*, user, provider, subject, verified=True):
    """Attach `(provider, subject)` to `user`.

    Refuses if that subject already belongs to anyone — including `user`, where
    a second link would be meaningless — and if `user` already has an identity
    with this provider.

    The IntegrityError branch is not defensive padding: the check-then-insert
    above it is a race, and two concurrent sign-ins with the same Google account
    is exactly when it loses. The database constraint is the real guarantee and
    this translates its error into one a caller can act on.
    """
    owner = find_by_identity(provider, subject)
    if owner is not None:
        if owner.pk == user.pk:
            raise Conflict("That account is already linked to you.")
        # Deliberately does not say whose. Naming the other account would turn
        # this into an oracle for "does X have a Google account linked".
        raise Conflict("That account is already linked to a different user.")

    if AuthIdentity.objects.filter(user=user, provider=provider).exists():
        raise Conflict(
            f"Your account already has a {provider} sign-in linked. "
            f"Unlink it first to link a different one."
        )

    try:
        return AuthIdentity.objects.create(
            user=user,
            provider=provider,
            subject=subject,
            verified_at=timezone.now() if verified else None,
        )
    except IntegrityError:
        # Lost the race described above.
        raise Conflict("That account is already linked to a different user.")


def unlink_identity(*, user, provider):
    """Remove a linked provider.

    Two refusals, both about not stranding people:

    * The last identity. An account nobody can sign into is a support ticket
      forever, and there is no admin path to restore access.
    * The canonical phone. It is the identity the platform routes on; removing it
      while it is canonical would leave a user whose account is keyed to a number
      they no longer control.
    """
    # A provider we have never heard of is a caller bug, not a missing link.
    # Without this, `unlinkProvider(provider: "gogle")` answers "that is not
    # linked to your account" — which is true, unhelpful, and sends the client
    # author looking at their account state instead of their spelling.
    if provider not in dict(AuthIdentity.PROVIDER_CHOICES):
        raise ValidationError(f"Unknown sign-in method '{provider}'.")

    identity = AuthIdentity.objects.filter(user=user, provider=provider).first()
    if identity is None:
        raise NotFound("That sign-in method is not linked to your account.")

    if AuthIdentity.objects.filter(user=user).count() <= 1:
        raise Conflict(
            "That is your only way to sign in. Link another method before "
            "removing this one."
        )

    if provider == AuthIdentity.PROVIDER_PHONE and user.phone_e164:
        raise Conflict(
            "Your phone is your account's identity and cannot be unlinked. "
            "Change your phone number instead."
        )

    identity.delete()
    return identity


@transaction.atomic
def sign_in_with_identity(*, provider, subject, email=None):
    """Resolve a verified identity to a user, creating one only if new.

    THE RULE THAT MATTERS: `email` is accepted for RECORD-KEEPING ONLY. It is
    never used to find an existing account.

    "Google says this email, we have a user with that email, log them in" is the
    tempting shortcut and it is an account-takeover primitive: our stored emails
    were never verified — `createUser` accepts any string — so registering with a
    victim's address and later signing in with Google for it would inherit their
    account. Matching on email is how that happens, so we do not match on email.

    A user who wants both must LINK them from an authenticated session, which
    proves they hold both sides at once. That is the only evidence this system
    accepts.
    """
    user = find_by_identity(provider, subject)
    if user is not None:
        AuthIdentity.objects.filter(provider=provider, subject=subject).update(
            last_used_at=timezone.now()
        )
        return user, False

    # New identity -> new account. Never an existing one, whatever the email says.
    user = User.objects.create_user(
        # Username stays required by AbstractUser; it is now an internal artefact
        # rather than an identity, so it is generated and never shown. Phase 5 of
        # the migration removes it from the public schema.
        username=_generate_username(provider, subject),
        email=email or '',
        password=None,  # no password: this account signs in via the provider
    )
    if provider == AuthIdentity.PROVIDER_PHONE:
        user.phone_e164 = subject
        user.phone_verified_at = timezone.now()
        user.save(update_fields=['phone_e164', 'phone_verified_at'])

    link_identity(user=user, provider=provider, subject=subject, verified=True)
    return user, True


def _generate_username(provider, subject):
    """An internal, unique, non-guessable username for a provider-created account.

    Never shown and never typed. It exists because `AbstractUser.USERNAME_FIELD`
    is `username` and `create_user` requires it — removing that is migration
    phase 5, not this sprint.

    Derived from the subject rather than random so it is stable and debuggable,
    hashed rather than raw so a phone number never leaks into a field that
    `usersPage(search:)` matches on.
    """
    import hashlib

    digest = hashlib.sha256(f'{provider}:{subject}'.encode()).hexdigest()[:16]
    return f'{provider}_{digest}'


def attach_phone(*, user, phone_e164):
    """Make a verified phone this user's canonical identity.

    Refuses if another account already holds it — the unique constraint would
    refuse anyway, but a caught IntegrityError cannot say which of several
    columns collided, and the caller needs to tell the user something specific.
    """
    clash = User.objects.filter(phone_e164=phone_e164).exclude(pk=user.pk).exists()
    if clash:
        # Same reasoning as link_identity: does not reveal whose.
        raise Conflict(
            "That phone number is already in use on another account."
        )

    with transaction.atomic():
        user.phone_e164 = phone_e164
        user.phone_verified_at = timezone.now()
        user.save(update_fields=['phone_e164', 'phone_verified_at'])

        # The phone is both the canonical identity AND a way to sign in, so it
        # exists in both places. They are different facts: the column answers
        # "who is this", the identity row answers "how did they prove it".
        identity = AuthIdentity.objects.filter(
            user=user, provider=AuthIdentity.PROVIDER_PHONE
        ).first()
        if identity is None:
            link_identity(
                user=user,
                provider=AuthIdentity.PROVIDER_PHONE,
                subject=phone_e164,
                verified=True,
            )
        else:
            identity.subject = phone_e164
            identity.verified_at = timezone.now()
            identity.save(update_fields=['subject', 'verified_at'])

    return user


def revoke_all_sessions(*, user):
    """Invalidate every JWT ever issued to this user (W7 §5b).

    All-or-nothing by design: stateless tokens cannot be enumerated, so there is
    nothing to revoke selectively. Per-device revocation needs refresh tokens —
    a W8 decision, deliberately not faked here with an "Active sessions" list
    that could only ever show one invented row.
    """
    User.objects.filter(pk=user.pk).update(token_version=models.F('token_version') + 1)
    user.refresh_from_db(fields=['token_version'])
    return user
