#!/usr/bin/env bash
# Restore a PostgreSQL database from a gzipped dump produced by backup_db.sh.
#
# Usage:
#   DATABASE_URL=postgres://user:pass@host:5432/db ./scripts/restore_db.sh backups/db_XXXX.sql.gz
#
# WARNING: this applies the dump to the target database. Take a fresh backup and
# confirm you are pointed at the intended (non-production or maintenance) target.
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL to the Postgres connection string}"
DUMP="${1:?Pass the path to a db_*.sql.gz dump}"
[ -f "$DUMP" ] || { echo "No such file: $DUMP" >&2; exit 1; }

read -r -p "Restore ${DUMP} into ${DATABASE_URL%%\?*}? [y/N] " confirm
[ "$confirm" = "y" ] || { echo "Aborted."; exit 1; }

echo "Restoring ${DUMP} ..."
gunzip -c "$DUMP" | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
echo "Restore complete. Run migrations if the dump predates schema changes:"
echo "  python manage.py migrate --noinput"
