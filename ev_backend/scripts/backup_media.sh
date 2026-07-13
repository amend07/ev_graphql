#!/usr/bin/env bash
# Media backup: archive the uploaded-files directory (station images, etc.).
#
# Usage:
#   MEDIA_ROOT=/app/media ./scripts/backup_media.sh [dest_dir]
#
# For object storage (S3/GCS) prefer native bucket versioning/replication instead.
set -euo pipefail

MEDIA_ROOT="${MEDIA_ROOT:-./media}"
DEST_DIR="${1:-./backups}"
mkdir -p "$DEST_DIR"

[ -d "$MEDIA_ROOT" ] || { echo "MEDIA_ROOT not found: $MEDIA_ROOT" >&2; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST_DIR}/media_${STAMP}.tar.gz"

echo "Archiving ${MEDIA_ROOT} to ${OUT} ..."
tar -czf "$OUT" -C "$(dirname "$MEDIA_ROOT")" "$(basename "$MEDIA_ROOT")"
echo "Done: ${OUT}"

RETENTION_DAYS="${RETENTION_DAYS:-14}"
find "$DEST_DIR" -name 'media_*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete || true
