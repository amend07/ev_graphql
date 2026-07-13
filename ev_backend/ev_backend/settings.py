
from pathlib import Path
from decouple import config
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# Loaded from the environment in production; the insecure literal is a
# local-dev fallback only. Set DJANGO_SECRET_KEY (and rotate it) for any deploy.
SECRET_KEY = config('DJANGO_SECRET_KEY', default='django-insecure-y8yd0nrw5l)wp^7gjmcv#%04pphh$5pio!1pzg06550g%w1h)e')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '10.0.2.2', '172.24.143.62', '0.0.0.0', '*']

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'graphene_django',
    'graphene_file_upload',
    'bookings',
    'accounts',
    'stations',
    'corsheaders',
]


CORS_ALLOW_HEADERS = [
    'authorization',
    'content-type',
    'x-csrftoken',
    'x-requested-with',
]

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True


GRAPHENE = {
    "SCHEMA": "ev_backend.schema.schema",  # You’ll define this file
    "MIDDLEWARE": [
        "graphql_jwt.middleware.JSONWebTokenMiddleware",
    ],
}

AUTHENTICATION_BACKENDS = [
    "graphql_jwt.backends.JSONWebTokenBackend",
    "django.contrib.auth.backends.ModelBackend",
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ev_backend.csrf_exempt.DisableCSRF',
]

ROOT_URLCONF = 'ev_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'ev_backend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Credential (PIN) validation
# The authentication credential is a 6-digit numeric PIN. The default Django
# validators (MinimumLengthValidator >= 8, NumericPasswordValidator) would
# reject a valid PIN, so the PIN policy is the single configured validator.
# It still runs through Django's password hasher — the PIN is never stored raw.

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'accounts.validators.SixDigitPINValidator',
    },
]


AUTH_USER_MODEL = 'accounts.User'

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


if DEBUG:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = config('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = f'EV Charging <{config("EMAIL_HOST_USER")}>'

# ---------------------------------------------------------------------------
# Sprint 3 — Authentication hardening
# ---------------------------------------------------------------------------
from datetime import timedelta

# OTP policy (Part 2)
OTP_LENGTH = config('OTP_LENGTH', default=6, cast=int)
OTP_VALIDITY_MINUTES = config('OTP_VALIDITY_MINUTES', default=10, cast=int)
OTP_MAX_ATTEMPTS = config('OTP_MAX_ATTEMPTS', default=5, cast=int)
OTP_REQUEST_COOLDOWN_SECONDS = config('OTP_REQUEST_COOLDOWN_SECONDS', default=60, cast=int)

# Brute-force protection (Part 3) — per-IP and per-account. window/lockout in seconds.
AUTH_RATELIMIT = {
    "LOGIN": {
        "limit": config('RL_LOGIN_LIMIT', default=10, cast=int),
        "window": config('RL_LOGIN_WINDOW', default=300, cast=int),
        "lockout": config('RL_LOGIN_LOCKOUT', default=900, cast=int),
    },
    "OTP_REQUEST": {
        "limit": config('RL_OTP_REQUEST_LIMIT', default=5, cast=int),
        "window": config('RL_OTP_REQUEST_WINDOW', default=3600, cast=int),
        "lockout": config('RL_OTP_REQUEST_LOCKOUT', default=3600, cast=int),
    },
    "OTP_VERIFY": {
        "limit": config('RL_OTP_VERIFY_LIMIT', default=10, cast=int),
        "window": config('RL_OTP_VERIFY_WINDOW', default=900, cast=int),
        "lockout": config('RL_OTP_VERIFY_LOCKOUT', default=900, cast=int),
    },
}

# Rate-limit / lockout state store. Local-memory by default (per-process);
# set REDIS_URL in production for a shared, cross-worker cache.
_REDIS_URL = config('REDIS_URL', default='')
if _REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": _REDIS_URL}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "auth-throttle"}}

# JWT hardening (Part 4). Explicit HS256, verified expiry, short-lived access
# tokens with refresh support. Header prefix stays "JWT" to match the Flutter
# client's AuthLink ("JWT <token>"). Secret is env-driven, falling back to
# SECRET_KEY so existing tokens keep verifying.
GRAPHQL_JWT = {
    "JWT_ALGORITHM": "HS256",
    "JWT_SECRET_KEY": config('JWT_SECRET_KEY', default=SECRET_KEY),
    "JWT_VERIFY_EXPIRATION": True,
    "JWT_EXPIRATION_DELTA": timedelta(minutes=config('JWT_ACCESS_MINUTES', default=30, cast=int)),
    "JWT_ALLOW_REFRESH": True,
    "JWT_REFRESH_EXPIRATION_DELTA": timedelta(days=config('JWT_REFRESH_DAYS', default=7, cast=int)),
    "JWT_AUTH_HEADER_PREFIX": "JWT",
}

# Structured authentication logging (Part 5). The accounts.auth_logging helper
# scrubs sensitive fields, so PINs/OTPs/tokens are never written here.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "auth": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "auth_console": {"class": "logging.StreamHandler", "formatter": "auth"},
    },
    "loggers": {
        "accounts.auth": {
            "handlers": ["auth_console"],
            "level": config('AUTH_LOG_LEVEL', default='INFO'),
            "propagate": False,
        },
    },
}
