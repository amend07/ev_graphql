from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
from accounts.models import User

@shared_task
def send_reset_otp_email(user_id: int, otp: str):
    try:
        user = User.objects.get(id=user_id)
        subject = "Your Password Reset OTP - EV Charging Station"
        
        txt_message = render_to_string("accounts/emails/reset_otp.txt", {
            "user": user, 
            "otp": otp
        })
        html_message = render_to_string("accounts/emails/reset_otp.html", {
            "user": user, 
            "otp": otp
        })
        
        send_mail(
            subject,
            txt_message,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_message,
            fail_silently=False,
        )
        return f"OTP email sent to {user.email}"
    except User.DoesNotExist:
        return "User not found"
    except Exception as e:
        return f"Error sending email: {str(e)}"