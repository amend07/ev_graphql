# Backup, Restore & Monitoring

## Database backups

Scripted with `scripts/backup_db.sh` (PostgreSQL, gzipped `pg_dump`):

```bash
DATABASE_URL=postgres://user:pass@host:5432/db ./scripts/backup_db.sh /var/backups/ev
```

- Produces `db_<UTC-timestamp>.sql.gz`.
- Retention: deletes dumps older than `RETENTION_DAYS` (default 14).
- **Schedule it** (pick one):
  - cron: `0 2 * * * DATABASE_URL=… /app/scripts/backup_db.sh /var/backups/ev`
  - Kubernetes `CronJob` running the same image/command
  - Managed-DB automated snapshots (RDS/Cloud SQL) — preferred; use the script
    as a portable secondary.

Store backups off-host (S3/GCS bucket with versioning) and encrypt at rest.

## Media backups

`scripts/backup_media.sh` archives `MEDIA_ROOT`:

```bash
MEDIA_ROOT=/app/media ./scripts/backup_media.sh /var/backups/ev
```

If media lives in object storage, prefer native bucket **versioning +
cross-region replication** over tar archives.

## Restore

```bash
# 1. Take a fresh safety backup of the target first.
# 2. Restore the dump:
DATABASE_URL=postgres://user:pass@host:5432/db ./scripts/restore_db.sh db_20240101T020000Z.sql.gz
# 3. Apply any newer migrations:
python manage.py migrate --noinput
```

The restore script prompts for confirmation and stops on the first error.
**Test restores regularly** — an untested backup is not a backup.

## Recovery notes

- The database is the source of truth for users, stations, bookings, reviews.
- Media (station images) are non-critical and regenerable by re-upload; still
  back them up to avoid broken links.
- The cache (Redis) is disposable — it rebuilds on demand. No backup needed.

## Monitoring recommendations

- **Uptime/health**: poll `/ready/` (DB + cache) and alert on non-200; `/live/`
  for liveness.
- **Metrics**: request rate, p50/p95 latency, 4xx/5xx rates, DB connection pool
  usage, Gunicorn worker saturation, Redis hit rate.
- **Logs**: ship stdout to a central store (Loki/CloudWatch/ELK). Watch the
  `accounts.auth` stream for `account_locked` / repeated `login_failure`
  (brute-force signal) and `ev_backend.graphql` for masked internal errors.
- **Alerts**: 5xx spike, `/ready/` failing, DB unreachable, backup job failure,
  TLS-cert expiry, disk usage on the media volume.
- **Security**: alert on bursts of `pin_reset_requested` (OTP abuse) and on
  rate-limit lockouts trending up.
