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

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='user')
    owner_status = models.CharField(
        max_length=20,
        choices=OWNER_STATUS_CHOICES,
        null=True,
        blank=True,
        default=None,
        help_text="Approval state for station owners. NULL for other roles.",
    )

    def is_station_owner(self):
        """Role check only — says nothing about approval. Clients rely on this
        to route owners, so its meaning must not change."""
        return self.role == 'station_owner'

    def is_approved_owner(self):
        """The check that actually gates station management."""
        return self.role == 'station_owner' and self.owner_status == self.OWNER_APPROVED

    def __str__(self):
        return f"{self.username} ({self.role})"


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
    ACTION_CHOICES = (
        (ACTION_USER_ACTIVATED, 'User activated'),
        (ACTION_USER_DEACTIVATED, 'User deactivated'),
        (ACTION_USER_DELETED, 'User deleted'),
        (ACTION_OWNER_APPROVED, 'Station owner approved'),
        (ACTION_OWNER_REJECTED, 'Station owner rejected'),
    )

    TARGET_USER = 'user'

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
