"""Pagination helpers (Sprint 5, Part 2).

Every collection endpoint runs through :func:`paginate` (proper pages, with a
total count) or :func:`hard_cap` (legacy list fields, bounded but not broken for
existing clients). No query is ever allowed to return an unbounded result set.
"""

from django.conf import settings


def clamp_page_size(size):
    """Clamp a requested page size to [1, GRAPHQL_MAX_PAGE_SIZE]."""
    default = settings.GRAPHQL_DEFAULT_PAGE_SIZE
    maximum = settings.GRAPHQL_MAX_PAGE_SIZE
    if size is None:
        return default
    try:
        size = int(size)
    except (TypeError, ValueError):
        return default
    return max(1, min(size, maximum))


def clamp_offset(offset):
    try:
        return max(0, int(offset or 0))
    except (TypeError, ValueError):
        return 0


def paginate(queryset, limit=None, offset=None):
    """Return ``(items, total_count, has_next)`` for a clamped window.

    ``total_count`` is computed with an efficient ``COUNT(*)`` before slicing so
    clients can page reliably.
    """
    size = clamp_page_size(limit)
    start = clamp_offset(offset)
    total = queryset.count()
    items = list(queryset[start:start + size])
    has_next = (start + size) < total
    return items, total, has_next


def hard_cap(queryset):
    """Bound a legacy (unpaginated) list field so it can never be unbounded."""
    return queryset[: settings.GRAPHQL_LIST_HARD_CAP]
