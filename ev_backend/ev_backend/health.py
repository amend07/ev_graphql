"""Operational endpoints (Sprint 5, Part 7).

Plain HTTP JSON endpoints suitable for load-balancer / orchestrator probes,
independent of the GraphQL layer:

    GET /health/   liveness  — process is up
    GET /ready/    readiness — database is reachable (503 if not)
    GET /version/  build version

Failure details are never leaked: readiness reports a coarse status only.
"""

import logging

from django.conf import settings
from django.db import connections
from django.http import JsonResponse

logger = logging.getLogger("ev_backend.health")


def health(request):
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})


def version(request):
    return JsonResponse({"version": settings.APP_VERSION})


def readiness(request):
    db_ok = True
    try:
        connection = connections["default"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pragma: no cover - exercised only on outage
        db_ok = False
        logger.error("Readiness DB check failed: %r", exc)

    body = {
        "status": "ready" if db_ok else "not_ready",
        "database": "ok" if db_ok else "unavailable",
        "version": settings.APP_VERSION,
    }
    return JsonResponse(body, status=200 if db_ok else 503)
