
from pathlib import Path
from decouple import config, Csv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY: secret key comes from the environment. The insecure literal is a
# dev-only fallback; production startup validation (end of file) rejects it.
SECRET_KEY = config('DJANGO_SECRET_KEY', default='django-insecure-y8yd0nrw5l)wp^7gjmcv#%04pphh$5pio!1pzg06550g%w1h)e')

# Debug is OFF by default. Development enables it with DJANGO_DEBUG=True (.env).
DEBUG = config('DJANGO_DEBUG', default=False, cast=bool)

# Permissive localhost set in dev; explicit, env-driven allow-list in production.
ALLOWED_HOSTS = config(
    'DJANGO_ALLOWED_HOSTS',
    default='127.0.0.1,localhost,0.0.0.0,10.0.2.2' if DEBUG else '',
    cast=Csv(),
)

MEDIA_URL = '/media/'
MEDIA_ROOT = config('MEDIA_ROOT', default=os.path.join(BASE_DIR, 'media'))

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
    'notifications',
    'vehicles',
    'charging',
    'corsheaders',
]


CORS_ALLOW_HEADERS = [
    'authorization',
    'content-type',
    'x-csrftoken',
    'x-requested-with',
]

# CORS: allow any origin only in development; production uses an explicit list
# supplied via CORS_ALLOWED_ORIGINS (comma-separated, supports multiple frontends).
CORS_ALLOW_ALL_ORIGINS = DEBUG
if not DEBUG:
    CORS_ALLOWED_ORIGINS = config('CORS_ALLOWED_ORIGINS', default='', cast=Csv())
CORS_ALLOW_CREDENTIALS = config('CORS_ALLOW_CREDENTIALS', default=False, cast=bool)

# Trusted origins for CSRF-protected views (admin, browsable clients).
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=Csv())


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
    'ev_backend.security_headers.SecurityHeadersMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ev_backend.csrf_exempt.DisableCSRF',
]

# Serve static files via WhiteNoise in production when the package is installed
# (see requirements.txt). Skipped gracefully in local dev environments without it.
try:
    import whitenoise  # noqa: F401
    MIDDLEWARE.insert(
        MIDDLEWARE.index('django.middleware.security.SecurityMiddleware') + 1,
        'whitenoise.middleware.WhiteNoiseMiddleware',
    )
except ImportError:
    pass

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

# Production databases are configured via DATABASE_URL (e.g. Postgres); local
# development falls back to SQLite. conn_max_age keeps connections pooled.
DATABASE_URL = config('DATABASE_URL', default='')
if DATABASE_URL:
    import dj_database_url
    DATABASES = {
        'default': dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=config('DB_CONN_MAX_AGE', default=600, cast=int),
            ssl_require=config('DB_SSL_REQUIRE', default=False, cast=bool),
        )
    }
else:
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
STATIC_ROOT = config('STATIC_ROOT', default=os.path.join(BASE_DIR, 'staticfiles'))

# Compressed, cache-busted static files via WhiteNoise when installed.
try:
    import whitenoise  # noqa: F401
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
except ImportError:
    pass

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Email is fully env-driven with safe defaults so settings import even without a
# .env (needed for CI/tests). Console backend in dev; SMTP in production.
EMAIL_BACKEND = config(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.console.EmailBackend' if DEBUG
    else 'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config(
    'DEFAULT_FROM_EMAIL',
    default=f'EV Charging <{EMAIL_HOST_USER or "no-reply@example.com"}>',
)

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
    # Session revocation (W7 §5b). The payload handler stamps the user's
    # `token_version` into every token; the decode handler refuses tokens whose
    # stamp no longer matches. See accounts/jwt.py for why it must be the decode
    # handler and not the user lookup.
    #
    # Both are needed. With only the payload handler, tokens would carry a claim
    # nothing reads. With only the decode handler, every token would look like
    # generation 0 forever and `logoutEverywhere` would revoke nothing.
    "JWT_PAYLOAD_HANDLER": "accounts.jwt.jwt_payload",
    "JWT_DECODE_HANDLER": "accounts.jwt.jwt_decode",
}

# ── Identity & phone verification (Sprint W7) ─────────────────────────────
#
# Region for numbers typed without a country code. A business fact (where the
# platform operates), not a constant — see accounts/phone.py.
PHONE_DEFAULT_COUNTRY_CODE = config('PHONE_DEFAULT_COUNTRY_CODE', default='251')

PHONE_OTP_LENGTH = config('PHONE_OTP_LENGTH', default=6, cast=int)
PHONE_OTP_VALIDITY_MINUTES = config('PHONE_OTP_VALIDITY_MINUTES', default=10, cast=int)
PHONE_OTP_MAX_ATTEMPTS = config('PHONE_OTP_MAX_ATTEMPTS', default=5, cast=int)
PHONE_OTP_REQUEST_COOLDOWN_SECONDS = config('PHONE_OTP_REQUEST_COOLDOWN_SECONDS', default=60, cast=int)

# SMS delivery adapter: 'disabled' (refuses, loudly) or 'console' (logs the code).
#
# Defaults to 'disabled' rather than 'console' even in development, because the
# default has to be the one that is safe when someone forgets it exists. There is
# no real provider yet; see accounts/sms.py and IDENTITY_ARCHITECTURE.md §9.2.
SMS_BACKEND = config('SMS_BACKEND', default='disabled')

# Social sign-in (W8). Comma-separated OAuth client IDs that a token's `aud` must
# match — the anti-forgery control in accounts/social.py. Empty (the default)
# means the provider is not configured: verification refuses and authCapabilities
# reports it false, so no client offers the button. A deployment enables Google
# or Apple purely by setting these; nothing else toggles.
#
# Google typically needs one ID per client platform (web, Android, iOS) — list
# all of them. Apple's is the Services ID (web) and/or the app's bundle ID.
GOOGLE_OAUTH_CLIENT_IDS = config('GOOGLE_OAUTH_CLIENT_IDS', default='', cast=Csv())
APPLE_CLIENT_IDS = config('APPLE_CLIENT_IDS', default='', cast=Csv())

# Structured authentication logging (Part 5). The accounts.auth_logging helper
# scrubs sensitive fields, so PINs/OTPs/tokens are never written here.
# All handlers write to stdout, which is log-rotation friendly (the container/
# platform captures and rotates stdout). Security-relevant loggers are separated
# from application logs. Sensitive values are scrubbed before logging by the
# accounts.auth_logging helper — PINs/OTPs/JWTs/secrets are never written.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "%(asctime)s %(levelname)s %(name)s [%(process)d] %(message)s"},
        "auth": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "security_console": {"class": "logging.StreamHandler", "formatter": "auth"},
    },
    "root": {"handlers": ["console"], "level": config('LOG_LEVEL', default='INFO')},
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": config('DJANGO_LOG_LEVEL', default='INFO'),
            "propagate": False,
        },
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "ev_backend": {
            "handlers": ["console"],
            "level": config('APP_LOG_LEVEL', default='INFO'),
            "propagate": False,
        },
        # --- security-relevant loggers ---
        "accounts.auth": {
            "handlers": ["security_console"],
            "level": config('AUTH_LOG_LEVEL', default='INFO'),
            "propagate": False,
        },
        "ev_backend.graphql": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "ev_backend.security": {"handlers": ["security_console"], "level": "INFO", "propagate": False},
        "ev_backend.health": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Sprint 4 — Business rules, validation & limits (all configurable)
# ---------------------------------------------------------------------------

# Booking (Part 2)
BOOKING_MIN_DURATION_MINUTES = config('BOOKING_MIN_DURATION_MINUTES', default=15, cast=int)
BOOKING_MAX_DURATION_HOURS = config('BOOKING_MAX_DURATION_HOURS', default=4, cast=int)
# Owners booking their own station is disallowed by default (business rule).
BOOKING_ALLOW_OWNER_SELF_BOOKING = config('BOOKING_ALLOW_OWNER_SELF_BOOKING', default=False, cast=bool)

# Charging sessions (W9).
# Walk-up (charging without an approved booking) is OFF by default: a session
# must be authorised by an approved booking for the station.
CHARGING_ALLOW_WALKUP = config('CHARGING_ALLOW_WALKUP', default=False, cast=bool)
# Simulated charger: seconds of charging until a "full" auto-complete. Real
# hardware reports full itself; this only governs the SimulatedChargerGateway.
CHARGING_SIM_FULL_SECONDS = config('CHARGING_SIM_FULL_SECONDS', default=3600, cast=int)

# Station (Part 3) — upper bounds for sanity/DoS protection.
STATION_MAX_DESCRIPTION_LENGTH = config('STATION_MAX_DESCRIPTION_LENGTH', default=2000, cast=int)
STATION_MAX_POWER_KW = config('STATION_MAX_POWER_KW', default=1000, cast=int)
STATION_MAX_PRICE_PER_KWH = config('STATION_MAX_PRICE_PER_KWH', default=10000, cast=int)
STATION_MAX_CHARGERS = config('STATION_MAX_CHARGERS', default=1000, cast=int)
# Optional allow-list of charger types; empty = accept any non-blank value
# (keeps compatibility with values the Flutter client already sends).
STATION_CHARGER_TYPES = [t for t in config('STATION_CHARGER_TYPES', default='').split(',') if t.strip()]

# Review (Part 4)
REVIEW_MIN_RATING = 1
REVIEW_MAX_RATING = 5
REVIEW_MAX_COMMENT_LENGTH = config('REVIEW_MAX_COMMENT_LENGTH', default=1000, cast=int)
# Require a completed ("done") booking before a user may review a station.
REVIEW_REQUIRE_COMPLETED_BOOKING = config('REVIEW_REQUIRE_COMPLETED_BOOKING', default=True, cast=bool)

# ---------------------------------------------------------------------------
# Sprint 5 — Scalability, reliability & performance
# ---------------------------------------------------------------------------

APP_VERSION = config('APP_VERSION', default='1.0.0')

# Pagination (Part 2)
GRAPHQL_DEFAULT_PAGE_SIZE = config('GRAPHQL_DEFAULT_PAGE_SIZE', default=20, cast=int)
GRAPHQL_MAX_PAGE_SIZE = config('GRAPHQL_MAX_PAGE_SIZE', default=100, cast=int)
# Hard cap for the legacy (unpaginated) list fields so no query is ever unbounded.
GRAPHQL_LIST_HARD_CAP = config('GRAPHQL_LIST_HARD_CAP', default=500, cast=int)

# Query complexity protection (Part 5)
GRAPHQL_MAX_DEPTH = config('GRAPHQL_MAX_DEPTH', default=12, cast=int)
# Reject oversized request bodies before parsing (bytes). Also guards file uploads.
DATA_UPLOAD_MAX_MEMORY_SIZE = config('DATA_UPLOAD_MAX_MEMORY_SIZE', default=5 * 1024 * 1024, cast=int)
DATA_UPLOAD_MAX_NUMBER_FIELDS = config('DATA_UPLOAD_MAX_NUMBER_FIELDS', default=1000, cast=int)

# Caching (Part 6) — public station list only; never user-specific data.
STATION_LIST_CACHE_SECONDS = config('STATION_LIST_CACHE_SECONDS', default=30, cast=int)

# ---------------------------------------------------------------------------
# Sprint 6 — Production readiness & deployment hardening
# ---------------------------------------------------------------------------

# --- Security headers (Part 2) — safe in dev; HTTPS-only ones gated on prod ---
SECURE_CONTENT_TYPE_NOSNIFF = True                       # X-Content-Type-Options
SECURE_REFERRER_POLICY = config('SECURE_REFERRER_POLICY', default='strict-origin-when-cross-origin')
X_FRAME_OPTIONS = config('X_FRAME_OPTIONS', default='DENY')
PERMISSIONS_POLICY = config('PERMISSIONS_POLICY', default='geolocation=(), microphone=(), camera=()')

if not DEBUG:
    # Behind a TLS-terminating proxy/load balancer.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
    SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True, cast=bool)
    SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=True, cast=bool)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = config('SESSION_COOKIE_SAMESITE', default='Lax')
    CSRF_COOKIE_SAMESITE = config('CSRF_COOKIE_SAMESITE', default='Lax')

# --- Fail-fast environment validation (Part 4) ---
# In production, refuse to start if required secure configuration is missing.
# Skipped while running the test suite so `manage.py test` works without a full
# production environment.
import sys as _sys

_RUNNING_TESTS = 'test' in _sys.argv
if (not DEBUG and not _RUNNING_TESTS
        and config('DJANGO_SKIP_PROD_CHECK', default=False, cast=bool) is False):
    from django.core.exceptions import ImproperlyConfigured

    _problems = []
    if SECRET_KEY.startswith('django-insecure'):
        _problems.append('DJANGO_SECRET_KEY must be a strong, unique secret (not the dev default).')
    if not ALLOWED_HOSTS:
        _problems.append('DJANGO_ALLOWED_HOSTS must list the production host(s).')
    if str(GRAPHQL_JWT.get('JWT_SECRET_KEY', '')).startswith('django-insecure'):
        _problems.append('JWT_SECRET_KEY (or DJANGO_SECRET_KEY) must be a strong secret.')
    if EMAIL_BACKEND.endswith('smtp.EmailBackend') and not (EMAIL_HOST_USER and EMAIL_HOST_PASSWORD):
        _problems.append('EMAIL_HOST_USER/EMAIL_HOST_PASSWORD are required for SMTP email (OTP delivery).')
    if not DATABASE_URL and DATABASES['default']['ENGINE'].endswith('sqlite3'):
        _problems.append('DATABASE_URL must point at a production database (SQLite is not supported in production).')
    # A per-process cache (LocMemCache) is worse than useless here: the rate-limit
    # and lockout counters live in this cache, so with N Gunicorn workers the
    # effective login/OTP limits become ~N× and lockouts never propagate —
    # silently defeating brute-force protection on the 6-digit PIN. Require a
    # SHARED cache in production, not merely "a cache".
    _cache_backend = CACHES.get('default', {}).get('BACKEND', '')
    if not _cache_backend or _cache_backend.endswith('locmem.LocMemCache'):
        _problems.append(
            'A SHARED cache backend is required in production (set REDIS_URL). '
            'The default per-process LocMemCache makes rate-limiting per-worker, '
            'which defeats brute-force/OTP protection.'
        )
    # ConsoleSmsBackend writes the verification code in clear text to the log.
    # On a real deploy that puts a live credential into the log aggregator, where
    # it is readable by everyone with log access and retained for as long as logs
    # are — while the user believes their phone is a second factor. 'disabled' is
    # allowed here: refusing to send is honest and is the current expected state.
    if SMS_BACKEND == 'console':
        _problems.append(
            "SMS_BACKEND='console' logs verification codes in clear text and must "
            "not be used in production. Use 'disabled' until a real provider is configured."
        )

    if _problems:
        raise ImproperlyConfigured(
            'Refusing to start: production configuration is incomplete.\n- '
            + '\n- '.join(_problems)
            + '\n(Set DJANGO_DEBUG=True for local development, or fix the above for production.)'
        )
