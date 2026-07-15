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
    """Bound a legacy (unpaginated) list field so it can never be unbounded.

    Truncates SILENTLY: the caller cannot tell a complete answer from a cut-off
    one. Only for list fields that shipped before pagination existed. New fields
    use :func:`paginate`.
    """
    return queryset[: settings.GRAPHQL_LIST_HARD_CAP]


def window(queryset, limit=None, offset=None):
    """Bound a bare list field: an explicit page when asked, else the hard cap.

    Consolidated in B3. This existed three times — once named `_window` in
    `stations.schema`, and twice inlined verbatim in `bookings.schema` — which is
    three chances to get a bound wrong and one place a fix would have been missed.
    Unlike :func:`paginate` it returns no total, so it cannot say `hasNext`; that
    is the price of the legacy list shape and the reason not to add more of them.
    """
    if limit is not None:
        start = clamp_offset(offset)
        return queryset[start:start + clamp_page_size(limit)]
    return hard_cap(queryset)


def apply_ordering(queryset, order_by, allowed, default, tie_break='id'):
    """Order by an allow-listed key, always tie-broken on a unique column.

    Two rules that were being re-typed at five call sites, where either is easy to
    forget and neither fails visibly:

    * **Allow-list, never the raw argument.** A free-form `order_by` lets a caller
      sort by `password` and read the hash out one comparison at a time.
    * **Tie-break on the primary key.** `-created_at` and `-date_joined` collide
      for rows written in the same instant, and an unstable sort silently repeats
      or drops rows *between pages* — which looks like a frontend bug for weeks.

    An unrecognised key falls back to `default` rather than erroring: ordering is
    a preference, and a typo should not take the screen down.

    ``tie_break`` is a parameter rather than a constant because `auditLogsPage`
    ties on `-id` to keep the newest row first within one timestamp. Either
    direction is stable, which is all paging needs — so the caller's existing
    choice is preserved rather than quietly normalised.
    """
    return queryset.order_by(allowed.get(order_by or 'newest', default), tie_break)
