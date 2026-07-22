"""Public station-list caching (Sprint 5, Part 6).

Only NON-user-specific data is cached: the base rows for active stations
(identity, location, charger info, aggregate rating). The per-user ``is_favorite``
flag is never cached — it is overlaid per request by the resolver.

Invalidation is version-keyed: any station or review write bumps a version
counter, so a subsequent read misses the old key and recomputes. A short TTL is
kept as a backstop for multi-process caches without shared invalidation.
"""

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count
from django.db.models.functions import Coalesce

_VERSION_KEY = "stations:list:version"


def _version():
    version = cache.get(_VERSION_KEY)
    if version is None:
        cache.set(_VERSION_KEY, 1, None)  # counter never expires on its own
        return 1
    return version


def invalidate_station_list():
    """Bump the version so all previously cached station lists are bypassed."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, 1, None)


def get_public_station_rows():
    """Cached list of base (non-user) rows for active stations, bounded by the
    legacy hard cap so the cache entry can never grow unbounded."""
    key = f"stations:list:{_version()}"
    rows = cache.get(key)
    if rows is not None:
        return rows

    from .models import Station, review_stats

    # Hidden reviews are excluded here too (B2.1). This list is cached, so a
    # moderated review left counting here would outlive the moderation by the
    # cache TTL on the single most-read endpoint on the platform. `post_save` on
    # Review bumps the version key, so hiding one recomputes this immediately.
    qs = (
        Station.objects.filter(is_active=True)
        .annotate(**review_stats())
        .order_by("id")[: settings.GRAPHQL_LIST_HARD_CAP]
    )

    rows = [
        {
            "station_id": s.id,
            "name": s.name,
            "latitude": s.latitude,
            "longitude": s.longitude,
            "charger_type": s.charger_type,
            "charge_mode": s.charge_mode,
            "num_of_charger": s.num_of_charger,
            "num_of_rate": s.num_of_rate,
            "average_rate": round(s.average_rate, 1),
            "power_output_kw": s.power_output_kw,
            "price_per_kwh": str(s.price_per_kwh),
            # Cached list is active-only, so this is always True here — included
            # so StationListType built from a cache row still carries the field.
            "is_active": s.is_active,
            "image": s.image.url if s.image else "",
        }
        for s in qs
    ]
    cache.set(key, rows, settings.STATION_LIST_CACHE_SECONDS)
    return rows
