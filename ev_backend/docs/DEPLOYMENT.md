# Deployment Guide

Production backend for the EV Charging platform (Django + GraphQL). This guide
covers configuration, running with Docker/Gunicorn, and static/media handling.

## 1. Prerequisites

- Python 3.12 (for non-Docker runs)
- PostgreSQL 14+ (production database)
- Redis 6+ (shared cache for rate limits and the station-list cache)
- A TLS-terminating reverse proxy / load balancer (nginx, ALB, etc.)

## 2. Configuration

All configuration is via environment variables (12-factor). Copy the template
and fill it in:

```bash
cp .env.example .env
```

**The app refuses to start in production (`DJANGO_DEBUG=False`) if required
secure configuration is missing** — see the fail-fast list in
`docs/PRODUCTION_CHECKLIST.md`.

### Environment variables

| Variable | Required (prod) | Default | Purpose |
|---|---|---|---|
| `DJANGO_DEBUG` | — | `False` | Enable dev mode. **Never `True` in production.** |
| `DJANGO_SECRET_KEY` | ✅ | dev fallback | Django signing key. Generate a strong random value. |
| `DJANGO_ALLOWED_HOSTS` | ✅ | localhost (dev) | Comma-separated hostnames. |
| `DATABASE_URL` | ✅ | SQLite (dev) | e.g. `postgres://user:pass@host:5432/db`. |
| `DB_SSL_REQUIRE` | — | `False` | Require TLS to the database. |
| `DB_CONN_MAX_AGE` | — | `600` | Persistent connection lifetime (s). |
| `REDIS_URL` | recommended | LocMem | Shared cache; required for correct multi-process rate limiting/caching. |
| `JWT_SECRET_KEY` | — | `DJANGO_SECRET_KEY` | JWT signing key. |
| `JWT_ACCESS_MINUTES` / `JWT_REFRESH_DAYS` | — | `30` / `7` | Token lifetimes. |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | ✅ (SMTP) | empty | OTP email delivery. |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` | — | Gmail/587/True | SMTP server. |
| `DEFAULT_FROM_EMAIL` | — | derived | From address. |
| `CORS_ALLOWED_ORIGINS` | ✅ (web) | empty | Comma-separated frontend origins. |
| `CORS_ALLOW_CREDENTIALS` | — | `False` | Allow credentialed CORS. |
| `CSRF_TRUSTED_ORIGINS` | — | empty | Trusted origins for admin/CSRF. |
| `SECURE_SSL_REDIRECT` | — | `True` (prod) | Redirect HTTP→HTTPS. |
| `SECURE_HSTS_SECONDS` | — | `31536000` | HSTS max-age. |
| `STATIC_ROOT` / `MEDIA_ROOT` | — | `staticfiles/` / `media/` | Collected static / uploads. |
| `APP_VERSION` | — | `1.0.0` | Reported by `/version/`. |
| `LOG_LEVEL` / `DJANGO_LOG_LEVEL` / `APP_LOG_LEVEL` / `AUTH_LOG_LEVEL` | — | `INFO` | Log levels. |

Business-rule knobs (booking durations, PIN/OTP/rate-limit thresholds, pagination
sizes) are documented inline in `ev_backend/settings.py` and all have safe defaults.

The Flutter client is unaffected by these — no GraphQL field, argument, or auth
header changed in this sprint.

## 3. Run with Docker (recommended)

Local/staging full stack (web + Postgres + Redis):

```bash
cp .env.example .env      # set DJANGO_SECRET_KEY etc.
docker compose up --build
```

The image is multi-stage and runs as a non-root user. The entrypoint applies
migrations and collects static before starting Gunicorn.

Production (managed DB/cache, image behind your LB):

```bash
docker build -t ev-backend:$(git rev-parse --short HEAD) ev_backend
docker run -p 8000:8000 --env-file prod.env ev-backend:<tag>
```

## 4. Run without Docker

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn ev_backend.wsgi:application -c gunicorn.conf.py
```

## 5. Static & media files

- **Static**: collected to `STATIC_ROOT` and served by **WhiteNoise**
  (compressed, cache-busted) — no separate static server needed.
- **Media** (uploaded station images): served from `MEDIA_ROOT`. For scale,
  point uploads at object storage (S3/GCS) via a storage backend and serve via
  CDN; back it up per `docs/BACKUP_RESTORE.md`.

## 6. Health probes

| Endpoint | Use |
|---|---|
| `GET /health/`, `GET /live/` | Liveness (process up) |
| `GET /ready/` | Readiness — checks DB + cache, returns `503` if down |
| `GET /version/` | Build version |

Wire `/live/` to the liveness probe and `/ready/` to the readiness probe.
