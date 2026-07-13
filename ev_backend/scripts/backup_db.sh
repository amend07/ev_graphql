#!/usr/bin/env bash
# Database backup (PostgreSQL). Produces a timestamped, compressed dump.
#
# Usage:
#   DATABASE_URL=postgres://user:pass@host:5432/db ./scripts/backup_db.sh [dest_dir]
#
# Restore with scripts/restore_db.sh. Schedule via cron/systemd-timer/k8s CronJob.
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL to the Postgres connection string}"
DEST_DIR="${1:-./backups}"
mkdir -p "$DEST_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST_DIR}/db_${STAMP}.sql.gz"

echo "Backing up database to ${OUT} ..."
pg_dump "$DATABASE_URL" --no-owner --no-privileges | gzip > "$OUT"
echo "Done: ${OUT}"

# Optional retention: delete dumps older than RETENTION_DAYS (default 14).
RETENTION_DAYS="${RETENTION_DAYS:-14}"
find "$DEST_DIR" -name 'db_*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete || true
