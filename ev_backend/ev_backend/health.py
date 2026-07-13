"""Operational endpoints (Sprint 5 Part 7 / Sprint 6 Part 6).

Plain HTTP JSON endpoints for load-balancer / orchestrator probes, independent
of the GraphQL layer:

    GET /health/   liveness  — process is up
    GET /live/     liveness  — alias, for k8s livenessProbe
    GET /ready/    readiness — database + cache reachable (503 if not)
    GET /version/  build version

Failure details are never leaked: readiness reports coarse status only.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse

logger = logging.getLogger("ev_backend.health")


def health(request):
    """Liveness: the process is up and able to serve."""
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})


# Alias so both /health/ and /live/ work as liveness probes.
liveness = health


def version(request):
    return JsonResponse({"version": settings.APP_VERSION})


def _check_database():
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception as exc:  # pragma: no cover - exercised only on outage
        logger.error("Readiness DB check failed: %r", exc)
        return False


def _check_cache():
    try:
        cache.set("readiness:probe", "1", 5)
        return cache.get("readiness:probe") == "1"
    except Exception as exc:  # pragma: no cover - exercised only on outage
        logger.error("Readiness cache check failed: %r", exc)
        return False


def readiness(request):
    """Readiness: verify the app can reach its critical dependencies."""
    db_ok = _check_database()
    cache_ok = _check_cache()
    ready = db_ok and cache_ok
    body = {
        "status": "ready" if ready else "not_ready",
        "database": "ok" if db_ok else "unavailable",
        "cache": "ok" if cache_ok else "unavailable",
        "version": settings.APP_VERSION,
    }
    return JsonResponse(body, status=200 if ready else 503)
