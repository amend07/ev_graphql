from django.db import migrations, models


class Migration(migrations.Migration):
    """Harden PasswordResetOTP: store a hash instead of the plaintext code and
    track verification attempts. Any in-flight (short-lived) codes are dropped."""

    dependencies = [
        ("accounts", "0002_passwordresetotp"),
    ]

    operations = [
        # Never store recoverable OTP values.
        migrations.RemoveField(
            model_name="passwordresetotp",
            name="otp",
        ),
        migrations.AddField(
            model_name="passwordresetotp",
            name="otp_hash",
            field=models.CharField(default="", max_length=128),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="passwordresetotp",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
