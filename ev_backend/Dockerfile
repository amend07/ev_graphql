# syntax=docker/dockerfile:1
# Multi-stage, non-root production image for the EV Charging backend.

# ---- Builder: install dependencies into a venv ----
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- Runtime: slim, non-root ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=ev_backend.settings

# libpq for psycopg at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser . /app

# Collect static at build time (WhiteNoise serves them). Uses a throwaway key so
# the build never needs real secrets; runtime overrides via env.
RUN DJANGO_DEBUG=True python manage.py collectstatic --noinput || true

RUN chmod +x /app/entrypoint.sh
USER appuser

EXPOSE 8000

# The entrypoint runs migrations then starts Gunicorn.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "ev_backend.wsgi:application", "-c", "gunicorn.conf.py"]
