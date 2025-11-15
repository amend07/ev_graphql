# accounts/models.py
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
import random

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

User = get_user_model()

class PasswordResetOTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    @classmethod
    def generate_for_user(cls, user: User) -> "PasswordResetOTP":
        cls.objects.filter(user=user).delete()
        otp = "".join([str(random.randint(0, 9)) for _ in range(6)])
        instance = cls.objects.create(
            user=user,
            otp=otp,
            expires_at=timezone.now() + timezone.timedelta(minutes=getattr(settings, 'OTP_VALIDITY_MINUTES', 10)),
        )
        subject = "Your EV-Backend Password Reset OTP"
        txt_message = render_to_string("accounts/emails/reset_otp.txt", {"user": user, "otp": otp})
        html_message = render_to_string("accounts/emails/reset_otp.html", {"user": user, "otp": otp})
        send_mail(
            subject=subject,
            message=txt_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
        )
        return instance

    def is_valid(self, supplied_otp: str) -> bool:
        return self.otp == supplied_otp and timezone.now() <= self.expires_at