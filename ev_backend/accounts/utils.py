from django.core.mail import send_mail
from django.conf import settings

def send_otp_email(user_email, otp_code):
    subject = 'Password Reset OTP - EV Charging Station'
    message = f'''
    Hello,
    
    You have requested to reset your password. Please use the following OTP to proceed:
    
    OTP: {otp_code}
    
    This OTP is valid for 10 minutes.
    
    If you didn't request this reset, please ignore this email.
    
    Best regards,
    EV Charging Station Team
    '''
    
    send_mail(
        subject,
        message,
        settings.DEFAULT_FROM_EMAIL,
        [user_email],
        fail_silently=False,
    )