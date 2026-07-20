from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
import secrets


class User(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('station_owner', 'Station Owner'),
        ('user', 'User'),
    )

    # Station-owner approval lifecycle (Sprint B1, Phase 2).
    #
    # Deliberately NOT ``is_active``: graphql_jwt rejects an inactive user's
    # token at decode time, so a "pending" owner modelled that way could not sign
    # in at all — not even to see that they are pending. Keeping approval on its
    # own field lets a pending owner use the app as a customer while station
    # management stays closed until an admin approves them.
    #
    # Only meaningful for ``role='station_owner'``; NULL for everyone else.
    OWNER_PENDING = 'pending'
    OWNER_APPROVED = 'approved'
    OWNER_REJECTED = 'rejected'
    OWNER_STATUS_CHOICES = (
        (OWNER_PENDING, 'Pending'),
        (OWNER_APPROVED, 'Approved'),
        (OWNER_REJECTED, 'Rejected'),
    )

    # ── Identity (Sprint W7) ────────────────────────────────────────────
    #
    # Phone is the CANONICAL identity: the thing that answers "who is this
    # person", not merely one way to log in. See docs/IDENTITY_ARCHITECTURE.md.
    #
    # NULLable, and that is forced by reality rather than preference: every
    # account that existed before W7 has no phone. A NOT NULL column here would
    # make the migration unrunnable. The consequence, recorded so nobody builds
    # on a false assumption: **phone is NOT guaranteed to be present**, and code
    # that treats it as guaranteed is a bug waiting for a legacy user.
    #
    # UNIQUE is what makes "one human, one account" a database guarantee rather
    # than an application check that a concurrent signup can race past.
    phone_e164 = models.CharField(
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        default=None,
        help_text="Canonical identity in E.164 (+CCXXXXXXXXX). NULL for pre-W7 accounts.",
    )
    # Separate from the number, and a timestamp rather than a boolean.
    #
    # A claimed phone and a PROVEN phone are different facts. This is the B1
    # owner_status lesson: encoding a workflow state in a field that means
    # something else produced a mutation that did nothing for two sprints. A
    # timestamp also survives a future policy that says proof expires; a boolean
    # would not.
    phone_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text="When this phone was proven. NULL means claimed but unproven.",
    )
    # Bumping this invalidates every JWT ever issued to this user (W7 §5b).
    #
    # The platform had NO way to revoke a session before this: the JWT is
    # stateless and graphql_jwt.refresh_token is not installed, so a leaked token
    # was valid for its full lifetime and could not be cut off. One integer buys
    # that back. It is all-or-nothing by design — per-device revocation needs
    # refresh tokens, which is a W8 decision.
    token_version = models.PositiveIntegerField(
        default=0,
        help_text="Incremented by logoutEverywhere; asserted in the JWT payload.",
    )

    # What to call this person (W8) — the fix §10a of IDENTITY_ARCHITECTURE.md
    # demanded before an SMS provider is configured.
    #
    # `username` is an identity artefact, not a name. A phone-registered account
    # never chose one, so `_generate_username` mints `phone_9f2c1a4b…` — and the
    # clients render it at the user ("Welcome back, phone_9f2c1a4b8e3d0f11").
    # This is the name a human chose; read it through `display_label`, which
    # falls back so no client ever has to reach for `username` again.
    #
    # Blank rather than NULL: "never chose one" and "chose an empty string" are
    # the same state, and blank keeps every read a `str`.
    display_name = models.CharField(
        max_length=60,
        blank=True,
        default='',
        help_text="Human-chosen name. Blank means never set — read display_label instead.",
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='user')
    owner_status = models.CharField(
        max_length=20,
        choices=OWNER_STATUS_CHOICES,
        null=True,
        blank=True,
        default=None,
        help_text="Approval state for station owners. NULL for other roles.",
    )

    # Who decided, when, and why (Sprint B2.1).
    #
    # B1 shipped the state machine but not its provenance: `owner_status` said an
    # application was rejected and nothing said who rejected it or on what
    # grounds. The audit trail recorded the event, but the trail is a log — the
    # owner's own record has to carry the outcome, because that is what the
    # rejected owner is shown and what an admin reopening the case reads.
    #
    # `reviewer` is SET_NULL for the same reason `AuditLog.actor` is: an admin can
    # be deleted, and the decision must survive them.
    reviewer = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='owner_decisions',
        help_text="Admin who approved or rejected this station owner. NULL if never reviewed.",
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When owner_status was last decided by an admin.",
    )
    rejection_reason = models.TextField(
        blank=True,
        default='',
        help_text="Why the owner application was rejected. Empty unless owner_status='rejected'.",
    )

    # Prefixes minted by `identity._generate_username` for provider-created
    # accounts. Kept as data rather than a regex so the check states exactly what
    # it excludes; `test_display_label_never_returns_the_username_artefact` pins
    # it against the real generator so the two cannot drift apart.
    _GENERATED_USERNAME_PREFIXES = ('phone_', 'google_', 'apple_')

    @property
    def display_label(self) -> str:
        """What to call this user — safe to render anywhere, always non-empty.

        THE GUARANTEE (W8, §10a): this never returns the internal username
        artefact. A phone account that has not chosen a name yet reads as its
        masked number (`+251•••••3344`) rather than `phone_9f2c1a4b8e3d0f11`.

        The masked number is computed here rather than stored at signup on
        purpose: stored, it would go stale the moment the user changes their
        number, and we would be showing an old number as a name.

        Order: the name they chose ▸ their masked phone ▸ the username they
        actually typed ▸ a neutral fallback. That last case is a provider account
        with no phone — an OAuth signup — and 'User' is deliberately dull: it is
        a prompt to set a name, not something anyone mistakes for their identity.
        """
        if self.display_name:
            return self.display_name
        if self.phone_e164:
            from .phone import mask_phone

            return mask_phone(self.phone_e164)
        if not self.username.startswith(self._GENERATED_USERNAME_PREFIXES):
            return self.username
        return 'User'

    @property
    def public_display_label(self) -> str:
        """What STRANGERS may call this user — a review's author, a station owner.

        Deliberately NOT `display_label`, and the difference is the whole point:
        that one falls back to the masked phone, which is correct on your own
        dashboard and wrong on a public review. `+251•••••3344` as a byline
        publishes the last four digits of a real number to anyone reading the
        page, next to whatever that review says. A partial number is still PII
        the user never chose to show.

        So the phone step is simply absent here: chosen name ▸ typed username ▸
        'User'. A phone-only account with no chosen name is anonymous in public,
        which is the safe end of the trade.
        """
        if self.display_name:
            return self.display_name
        if not self.username.startswith(self._GENERATED_USERNAME_PREFIXES):
            return self.username
        return 'User'

    def has_verified_phone(self):
        """The check that gates anything phone-dependent.

        Both fields, deliberately: a number with no verified_at is a claim, and
        a verified_at with no number should be impossible but is not worth
        trusting.
        """
        return bool(self.phone_e164) and self.phone_verified_at is not None

    def is_station_owner(self):
        """Role check only — says nothing about approval. Clients rely on this
        to route owners, so its meaning must not change."""
        return self.role == 'station_owner'

    def is_approved_owner(self):
        """The check that actually gates station management."""
        return self.role == 'station_owner' and self.owner_status == self.OWNER_APPROVED

    def __str__(self):
        return f"{self.username} ({self.role})"


class PhoneVerification(models.Model):
    """A phone OTP in flight (Sprint W7).

    Deliberately a sibling of ``PasswordResetOTP`` rather than a reuse of it:
    same hardening, different subject and different consequence. That one proves
    "you control this mailbox, so you may reset a PIN"; this one proves "you
    control this handset, so you may BE this account". Sharing a table would mean
    a code minted for one purpose could be spent on the other, which is a
    privilege escalation for the price of a typo.

    The security design is copied wholesale from PasswordResetOTP because that
    design is good: the plaintext code is never stored, verification is
    constant-time and attempt-limited, and the record dies on success, expiry or
    exhaustion. Do not re-derive it.
    """

    #: The number being proven — E.164, not a FK. The whole point is that we do
    #: not yet know whose phone this is: a sign-in, a signup and a link all start
    #: identically, and binding to a user here would presume the answer.
    phone_e164 = models.CharField(max_length=16, db_index=True)
    otp_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['phone_e164', '-created_at'], name='phoneverif_lookup_idx'),
        ]

    @staticmethod
    def validity_minutes():
        return getattr(settings, 'PHONE_OTP_VALIDITY_MINUTES', 10)

    @staticmethod
    def max_attempts():
        return getattr(settings, 'PHONE_OTP_MAX_ATTEMPTS', 5)

    @staticmethod
    def cooldown_seconds():
        return getattr(settings, 'PHONE_OTP_REQUEST_COOLDOWN_SECONDS', 60)

    @staticmethod
    def otp_length():
        return getattr(settings, 'PHONE_OTP_LENGTH', 6)

    @classmethod
    def generate_for_phone(cls, phone_e164):
        """Mint a fresh code for this number. Returns ``(instance, code)``.

        Returning the plaintext instead of sending it is the one deliberate
        divergence from ``PasswordResetOTP.generate_for_user``. That one calls
        ``_send_email`` on itself because there is exactly one way to deliver an
        email and Django owns it. Here delivery is a swappable adapter that can
        legitimately REFUSE (see sms.py), and the caller must be able to react to
        that refusal — by rolling this row back rather than leaving a cooldown
        behind for a code nobody received. A model that sent its own SMS would
        need to know which backend is configured and what to do when it declines,
        which is the service's job, not the row's.

        The plaintext lives only in the caller's local variable, only until it
        reaches the adapter. Only the hash is ever written.
        """
        cls.objects.filter(phone_e164=phone_e164).delete()  # invalidate any prior code

        # secrets, not random: this code is a credential for the length of its
        # validity window. `random` is a Mersenne Twister with observable output,
        # which would make the OTP decorative.
        code = "".join(secrets.choice("0123456789") for _ in range(cls.otp_length()))

        instance = cls.objects.create(
            phone_e164=phone_e164,
            otp_hash=make_password(code),
            expires_at=timezone.now() + timezone.timedelta(minutes=cls.validity_minutes()),
        )
        return instance, code

    @classmethod
    def can_request(cls, phone_e164):
        """Cooldown gate. Returns ``(allowed, seconds_remaining)``.

        Matters more here than for email: every send costs money to a number the
        caller chose, so the cooldown is a spend control as well as an abuse one.
        """
        latest = cls.objects.filter(phone_e164=phone_e164).order_by('-created_at').first()
        if latest is None:
            return True, 0
        elapsed = (timezone.now() - latest.created_at).total_seconds()
        cooldown = cls.cooldown_seconds()
        if elapsed < cooldown:
            return False, int(cooldown - elapsed)
        return True, 0

    def is_expired(self):
        return timezone.now() > self.expires_at

    def verify(self, supplied):
        """``"ok"`` | ``"expired"`` | ``"locked"`` | ``"invalid"``. Single-use."""
        if self.is_expired():
            self.delete()
            return 'expired'

        if check_password(str(supplied), self.otp_hash):
            self.delete()
            return 'ok'

        type(self).objects.filter(pk=self.pk).update(attempts=models.F('attempts') + 1)
        self.refresh_from_db(fields=['attempts'])
        if self.attempts >= self.max_attempts():
            self.delete()
            return 'locked'
        return 'invalid'

    def __str__(self):
        return f"PhoneVerification({self.phone_e164})"


class AuthIdentity(models.Model):
    """One way a user can prove who they are (Sprint W7).

    One user, many identities: a phone, a Google account, an Apple account, the
    legacy password. See docs/IDENTITY_ARCHITECTURE.md.

    ``UNIQUE(provider, subject)`` IS THE ANTI-DUPLICATE RULE, and it is a database
    constraint rather than an application check on purpose: "look up the subject,
    create if absent" is a check-then-insert race, and two concurrent sign-ins
    with the same Google account is exactly when it fires. A constraint cannot be
    raced.

    ``subject`` is the provider's opaque ID — Google's `sub`, Apple's `sub`, the
    E.164 number for phone. It is NEVER public: it is a stable cross-app
    identifier for that person at that provider, and exposing it leaks who our
    users are to anyone who can read the schema.
    """

    PROVIDER_PHONE = 'phone'
    PROVIDER_GOOGLE = 'google'
    PROVIDER_APPLE = 'apple'
    #: The legacy username+PIN login, modelled as a provider so that today's
    #: sign-in is one option among several rather than a special case the rest of
    #: the system has to know about.
    PROVIDER_PASSWORD = 'password'
    PROVIDER_CHOICES = (
        (PROVIDER_PHONE, 'Phone'),
        (PROVIDER_GOOGLE, 'Google'),
        (PROVIDER_APPLE, 'Apple'),
        (PROVIDER_PASSWORD, 'Password (legacy)'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='identities')
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    subject = models.CharField(
        max_length=255,
        help_text="The provider's opaque ID for this person. Never exposed publicly.",
    )
    verified_at = models.DateTimeField(null=True, blank=True, default=None)
    linked_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'subject'], name='unique_identity_per_provider',
            ),
            # A user cannot hold two Google accounts: "which one is you" has no
            # answer, and unlinking becomes ambiguous.
            models.UniqueConstraint(
                fields=['user', 'provider'], name='unique_provider_per_user',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'provider'], name='identity_user_provider_idx'),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.provider}"


class AuditLog(models.Model):
    """Persistent record of administrative actions (Sprint B1, Phase 4).

    Written only through :mod:`accounts.audit` — never directly from a resolver —
    so every action is recorded the same way and nothing depends on a caller
    remembering to log.

    Two deliberate denormalisations, both so a record outlives what it describes:

    * ``actor`` is SET_NULL with the username copied to ``actor_username``. An
      admin can be deleted; the trail of what they did must not vanish with them.
    * the target is NOT a foreign key. ``deleteUser`` is a hard cascading delete,
      so an FK would delete the very record proving the deletion happened. The
      type/id/label triple is a snapshot, not a live reference.
    """

    ACTION_USER_ACTIVATED = 'user_activated'
    ACTION_USER_DEACTIVATED = 'user_deactivated'
    ACTION_USER_DELETED = 'user_deleted'
    ACTION_OWNER_APPROVED = 'owner_approved'
    ACTION_OWNER_REJECTED = 'owner_rejected'
    # Sprint B2.1: station and review administration.
    ACTION_STATION_ACTIVATED = 'station_activated'
    ACTION_STATION_DEACTIVATED = 'station_deactivated'
    ACTION_REVIEW_HIDDEN = 'review_hidden'
    ACTION_REVIEW_RESTORED = 'review_restored'
    ACTION_REVIEW_DELETED = 'review_deleted'
    # Sprint B3: privilege changes. Deliberately not reachable over GraphQL — see
    # `accounts/management/commands/promote_admin.py` for why.
    ACTION_ADMIN_PROMOTED = 'admin_promoted'
    ACTION_CHOICES = (
        (ACTION_USER_ACTIVATED, 'User activated'),
        (ACTION_USER_DEACTIVATED, 'User deactivated'),
        (ACTION_USER_DELETED, 'User deleted'),
        (ACTION_OWNER_APPROVED, 'Station owner approved'),
        (ACTION_OWNER_REJECTED, 'Station owner rejected'),
        (ACTION_STATION_ACTIVATED, 'Station activated'),
        (ACTION_STATION_DEACTIVATED, 'Station deactivated'),
        (ACTION_REVIEW_HIDDEN, 'Review hidden'),
        (ACTION_REVIEW_RESTORED, 'Review restored'),
        (ACTION_REVIEW_DELETED, 'Review deleted'),
        (ACTION_ADMIN_PROMOTED, 'Promoted to administrator'),
    )

    TARGET_USER = 'user'
    TARGET_STATION = 'station'
    TARGET_REVIEW = 'review'

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='audit_actions',
    )
    actor_username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=40, choices=ACTION_CHOICES)
    target_type = models.CharField(max_length=40)
    target_id = models.CharField(max_length=64)
    target_label = models.CharField(max_length=150, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at', '-id')
        indexes = [
            models.Index(fields=['-created_at'], name='audit_created_idx'),
            models.Index(fields=['action'], name='audit_action_idx'),
            models.Index(fields=['target_type', 'target_id'], name='audit_target_idx'),
        ]

    def __str__(self):
        return f"{self.actor_username or 'system'} {self.action} {self.target_type}:{self.target_id}"


class PasswordResetOTP(models.Model):
    """Single-use, hashed reset code (OTP).

    The plaintext code is never stored: only a salted hash is persisted, so the
    value is unrecoverable from the database. Verification is constant-time,
    attempt-limited, and the record is invalidated on success or on exhausting
    the attempt budget. Timings (validity, attempts, request cooldown) are
    configurable via settings.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    otp_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)

    # --- configurable knobs (overridable in settings) ---
    @staticmethod
    def otp_length():
        return getattr(settings, "OTP_LENGTH", 6)

    @staticmethod
    def validity_minutes():
        return getattr(settings, "OTP_VALIDITY_MINUTES", 10)

    @staticmethod
    def max_attempts():
        return getattr(settings, "OTP_MAX_ATTEMPTS", 5)

    @staticmethod
    def cooldown_seconds():
        return getattr(settings, "OTP_REQUEST_COOLDOWN_SECONDS", 60)

    @classmethod
    def can_request(cls, user) -> tuple[bool, int]:
        """Cooldown gate. Returns ``(allowed, seconds_remaining)``."""
        latest = cls.objects.filter(user=user).order_by("-created_at").first()
        if latest is None:
            return True, 0
        elapsed = (timezone.now() - latest.created_at).total_seconds()
        cooldown = cls.cooldown_seconds()
        if elapsed < cooldown:
            return False, int(cooldown - elapsed)
        return True, 0

    @classmethod
    def generate_for_user(cls, user: "User") -> "PasswordResetOTP":
        """Create a fresh code, store only its hash, and email the code once."""
        cls.objects.filter(user=user).delete()  # invalidate any prior code

        digits = "0123456789"
        code = "".join(secrets.choice(digits) for _ in range(cls.otp_length()))

        instance = cls.objects.create(
            user=user,
            otp_hash=make_password(code),
            expires_at=timezone.now() + timezone.timedelta(minutes=cls.validity_minutes()),
        )
        # Plaintext is used transiently for delivery only, then discarded.
        instance._send_email(code)
        return instance

    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def verify(self, supplied_otp: str) -> str:
        """Constant-time, single-use verification with attempt limiting.

        Returns one of ``"ok"``, ``"expired"``, ``"locked"``, ``"invalid"``.
        The record is deleted on success, expiry, or attempt exhaustion.
        """
        if self.is_expired():
            self.delete()
            return "expired"

        # check_password compares against the stored hash in constant time.
        if check_password(str(supplied_otp), self.otp_hash):
            self.delete()  # single-use
            return "ok"

        # Wrong code: burn an attempt atomically and lock out when exhausted.
        type(self).objects.filter(pk=self.pk).update(attempts=models.F("attempts") + 1)
        self.refresh_from_db(fields=["attempts"])
        if self.attempts >= self.max_attempts():
            self.delete()
            return "locked"
        return "invalid"

    def _send_email(self, code: str) -> None:
        context = {"user": self.user, "otp": code}
        txt_message = render_to_string("accounts/emails/reset_otp.txt", context)
        html_message = render_to_string("accounts/emails/reset_otp.html", context)
        send_mail(
            subject="Your EV-Backend PIN Reset Code",
            message=txt_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[self.user.email],
            html_message=html_message,
            fail_silently=False,
        )

    def __str__(self):
        return f"OTP for {self.user.email} (expires: {self.expires_at})"
