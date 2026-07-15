"""Station and review administration service layer (Sprint B2.1).

The counterpart of :mod:`accounts.administration` for the two things an admin
moderates rather than the people who own them. Same contract, for the same
reason: resolvers orchestrate, this module decides, and every destructive action
records itself here — so a resolver cannot deactivate a station or hide a review
without an audit record, because it does not do the deactivating or the hiding.

Why this lives in ``stations`` and not ``accounts``: the rules are about stations
and reviews. ``accounts.administration`` already reaches across apps for the
deletion cascade and that is a wart, not a pattern to copy.
"""

from ev_backend.errors import NotFound, ValidationError

from accounts import audit
from accounts.models import AuditLog

from .models import Review, Station


def get_station(station_id):
    """Fetch the station an admin is acting on, including inactive ones."""
    try:
        return Station.objects.select_related('owner').get(pk=station_id)
    except (Station.DoesNotExist, ValueError, TypeError):
        raise NotFound("Station not found.")


def get_review(review_id):
    """Fetch the review a moderator is acting on, hidden or not."""
    try:
        return Review.objects.select_related('station', 'user').get(pk=review_id)
    except (Review.DoesNotExist, ValueError, TypeError):
        raise NotFound("Review not found.")


def set_station_active(*, actor, station, is_active, reason=None, request=None):
    """Activate or deactivate a station. Idempotent.

    This is the admin counterpart of the owner's ``deleteStation``, which soft
    deletes by clearing the same flag. B1 listed "no station reactivation path"
    as a known deviation: an owner who removed a station, or an admin who took
    one down, had no way back short of database access. Activation closes that.

    Existing bookings are deliberately left untouched. Deactivating a station
    withdraws it from discovery and stops new bookings; cancelling the bookings
    people have already made is a different decision with a customer-visible
    consequence, and nothing in this sprint was asked to make it.
    """
    if station.is_active == is_active:
        # Idempotent, but a no-op must not leave a misleading audit record
        # claiming a state change that never happened.
        return station

    station.is_active = is_active
    station.save(update_fields=['is_active'])

    audit.record_station_action(
        actor=actor,
        action=(
            AuditLog.ACTION_STATION_ACTIVATED if is_active
            else AuditLog.ACTION_STATION_DEACTIVATED
        ),
        target_station=station,
        request=request,
        owner_id=station.owner_id,
        owner_username=station.owner.username,
        reason=(reason or '').strip(),
    )
    return station


def set_review_hidden(*, actor, review, is_hidden, reason=None, request=None):
    """Hide or restore a review. Idempotent, and reversible by design.

    Hiding is the moderation action that should be reached for first: it removes
    the content from every public read and from the station's rating, and it can
    be undone when the report turns out to be wrong. ``delete_review`` cannot.
    """
    if review.is_hidden == is_hidden:
        return review

    review.is_hidden = is_hidden
    # post_save on Review bumps the public station-list cache version, so the
    # cached ratings recompute without the hidden row. That wiring predates this
    # sprint; the save is what triggers it.
    review.save(update_fields=['is_hidden'])

    audit.record_review_action(
        actor=actor,
        action=(
            AuditLog.ACTION_REVIEW_HIDDEN if is_hidden
            else AuditLog.ACTION_REVIEW_RESTORED
        ),
        target_review=review,
        request=request,
        station_id=review.station_id,
        author_id=review.user_id,
        author_username=review.user.username,
        rating=review.rating,
        reason=(reason or '').strip(),
    )
    return review


def delete_review(*, actor, review, reason, request=None):
    """Hard-delete a review as a moderator. A reason is mandatory.

    Distinct from the customer-facing ``deleteReview``, which is author-only and
    needs no justification — deleting your own words is your business. Destroying
    someone else's is not, and it is unrecoverable, so the reason is required and
    the content is snapshotted into the audit record before it goes: afterwards
    there is nothing left to explain what was removed or whether it should have
    been.
    """
    reason = (reason or '').strip()
    if not reason:
        raise ValidationError("A reason is required to delete another user's review.")

    audit.record_review_action(
        actor=actor,
        action=AuditLog.ACTION_REVIEW_DELETED,
        target_review=review,
        request=request,
        station_id=review.station_id,
        author_id=review.user_id,
        author_username=review.user.username,
        rating=review.rating,
        comment=review.comment,
        was_hidden=review.is_hidden,
        reason=reason,
    )
    review.delete()
