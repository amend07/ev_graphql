from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
import random
import string

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
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    @classmethod
    def generate_for_user(cls, user: User) -> "PasswordResetOTP":
        cls.objects.filter(user=user).delete()
        
        otp = ''.join(random.choices(string.digits, k=6))
        
        expires_at = timezone.now() + timezone.timedelta(
            minutes=getattr(settings, 'OTP_VALIDITY_MINUTES', 10)
        )
        
        instance = cls.objects.create(
            user=user,
            otp=otp,
            expires_at=expires_at,
        )
        
        subject = "Your EV-Backend Password Reset OTP"
        context = {"user": user, "otp": otp}
        
        try:
            txt_message = render_to_string("accounts/emails/reset_otp.txt", context)
            html_message = render_to_string("accounts/emails/reset_otp.html", context)
            
            send_mail(
                subject=subject,
                message=txt_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
        except Exception as e:
            print(f"Error sending OTP email: {e}")
        
        return instance

    def is_valid(self, supplied_otp: str) -> bool:
        """Check if OTP is valid and not expired"""
        return self.otp == supplied_otp and timezone.now() <= self.expires_at

    def __str__(self):
        return f"OTP for {self.user.email} (expires: {self.expires_at})"