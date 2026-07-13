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
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='user')

    def is_station_owner(self):
        return self.role == 'station_owner'

    def __str__(self):
        return f"{self.username} ({self.role})"


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
