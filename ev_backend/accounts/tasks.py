from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
from accounts.models import User

@shared_task
def send_reset_otp_email(user_id: int, otp: str):
    user = User.objects.get(id=user_id)
    subject = "Your EV-Backend Password Reset OTP"
    txt = render_to_string("accounts/emails/reset_otp.txt", {"user": user, "otp": otp})
    html = render_to_string("accounts/emails/reset_otp.html", {"user": user, "otp": otp})
    send_mail(subject, txt, settings.DEFAULT_FROM_EMAIL, [user.email], html_message=html)