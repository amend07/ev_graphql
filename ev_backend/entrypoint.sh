#!/usr/bin/env sh
# Production entrypoint: apply migrations, then exec the given command (Gunicorn).
set -e

echo "Running database migrations..."
python manage.py migrate --noinput

# Collect static in case the image was built without them (idempotent).
python manage.py collectstatic --noinput >/dev/null 2>&1 || true

echo "Starting: $*"
exec "$@"
