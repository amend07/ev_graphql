# Production & Security Checklist

## Startup fail-fast (enforced in code)

With `DJANGO_DEBUG=False`, the app **refuses to start** unless all of these hold
(`ev_backend/settings.py`, bottom):

- [ ] `DJANGO_SECRET_KEY` set to a strong secret (not the `django-insecure-…` dev default)
- [ ] `DJANGO_ALLOWED_HOSTS` lists the production host(s)
- [ ] `JWT_SECRET_KEY` (or `DJANGO_SECRET_KEY`) is a strong secret
- [ ] `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` configured when using SMTP
- [ ] `DATABASE_URL` points at a production database (SQLite rejected)
- [ ] A **shared** cache is configured (`REDIS_URL`) — required; prod will not boot on per-process LocMemCache

## Pre-deploy

- [ ] `python manage.py check --deploy` passes (no security warnings)
- [ ] `python manage.py makemigrations --check --dry-run` is clean
- [ ] `python manage.py migrate` applied
- [ ] `python manage.py collectstatic --noinput` run (or done in image build)
- [ ] CI green (lint, tests, migration check, deploy check, image build)

## Security

- [ ] `DEBUG=False` in production
- [ ] Secrets only in environment / secret manager — **never committed**
- [ ] **Rotate the previously committed Gmail app-password and any old
      `SECRET_KEY`** (they exist in git history; consider history scrubbing with
      `git filter-repo` and force-push, then invalidate the old credentials)
- [ ] `.env`, `db.sqlite3`, `__pycache__` untracked (see `.gitignore`)
- [ ] HTTPS enforced (`SECURE_SSL_REDIRECT`, HSTS) behind the LB
- [ ] Security headers present: HSTS, `X-Frame-Options: DENY`,
      `X-Content-Type-Options: nosniff`, Referrer-Policy, Permissions-Policy
- [ ] Secure + `HttpOnly` + `SameSite` cookies (auto in prod)
- [ ] CORS restricted to known frontend origins (`CORS_ALLOWED_ORIGINS`)
- [ ] GraphiQL/introspection disabled in prod (gated on `DEBUG`)
- [ ] GraphQL depth limit + request-size limits active (defaults set)
- [ ] Rate limiting backed by Redis (per-IP + per-account)
- [ ] Admin protected; strong admin credentials; consider IP-allowlisting `/admin/`
- [ ] Logs never contain PIN/OTP/JWT/secrets (scrubbed by `accounts.auth_logging`)

## Operational

- [ ] Liveness → `/live/`, readiness → `/ready/` wired to the orchestrator
- [ ] Centralised log collection from stdout
- [ ] Database + media backups scheduled and **restore tested** (see BACKUP_RESTORE.md)
- [ ] Error alerting on 5xx and on `/ready/` failures
- [ ] Gunicorn workers/timeouts tuned for instance size (`gunicorn.conf.py`)

## Flutter compatibility

- [ ] No GraphQL field/argument renamed or removed this sprint
- [ ] JWT auth header prefix unchanged (`JWT <token>`)
- [ ] Confirm the app's configured API origin is in `CORS_ALLOWED_ORIGINS`
      (mobile apps don't need CORS, but any web build does)
