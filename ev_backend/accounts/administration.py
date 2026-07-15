"""Administration service layer (Sprint B1, Phase 3).

Holds the business rules for administrative actions once, so the resolvers in
``schema.py`` stay thin — the same split ``services.py`` uses for credentials.

Everything destructive goes through here, which is what makes the safety
interlocks and the audit trail unskippable: a resolver cannot delete a user
without also recording it, because it does not do the deleting.
"""

from django.db import transaction
from django.db.models import Q

from ev_backend.errors import Conflict, NotFound, ValidationError

from . import audit
from .models import AuditLog, User


def get_target(user_id):
    """Fetch the user an admin is acting on."""
    try:
        return User.objects.get(pk=user_id)
    except (User.DoesNotExist, ValueError, TypeError):
        raise NotFound("User not found.")


def _active_admin_count(excluding=None):
    qs = User.objects.filter(role='admin', is_active=True)
    if excluding is not None:
        qs = qs.exclude(pk=excluding.pk)
    return qs.count()


def _assert_not_self(actor, target, action):
    if actor.pk == target.pk:
        raise Conflict(f"You cannot {action} your own account.")


def _assert_not_last_admin(target, action):
    """Refuse to remove the platform's last way in.

    Only ever triggers for an active admin: there is no API to create one (see
    `create_admin_user.py`), so locking out the last one is unrecoverable without
    shell access to the server.
    """
    if target.role == 'admin' and target.is_active and _active_admin_count(excluding=target) == 0:
        raise Conflict(
            f"You cannot {action} the last active administrator. "
            "Promote another administrator first."
        )


def deletion_summary(user):
    """What a hard delete of ``user`` would destroy.

    Every FK to User is ON DELETE CASCADE, and stations cascade in turn, so this
    walks the same edges the database would:

        user → stations → bookings + reviews + favourites (anyone's)
        user → their own bookings, reviews, favourites

    ``other_users_affected`` is the number this exists to surface: deleting one
    owner erases OTHER people's booking history, and nothing else in the API
    tells you that before you do it.
    """
    from bookings.models import Booking
    from stations.models import Favorite, Review, Station

    stations = Station.objects.filter(owner=user)
    bookings = Booking.objects.filter(Q(user=user) | Q(station__owner=user))
    reviews = Review.objects.filter(Q(user=user) | Q(station__owner=user))
    favorites = Favorite.objects.filter(Q(user=user) | Q(station__owner=user))

    others = set()
    others.update(bookings.exclude(user=user).values_list('user_id', flat=True))
    others.update(reviews.exclude(user=user).values_list('user_id', flat=True))
    others.discard(user.pk)

    return {
        'stations': stations.count(),
        'bookings': bookings.count(),
        'reviews': reviews.count(),
        'favorites': favorites.count(),
        'other_users_affected': len(others),
    }


def set_user_active(*, actor, target, is_active, request=None):
    """Activate or deactivate an account, with the interlocks applied."""
    if not is_active:
        # Deactivation is what locks people out; activation is always safe.
        _assert_not_self(actor, target, "deactivate")
        _assert_not_last_admin(target, "deactivate")

    if target.is_active == is_active:
        # Idempotent, but do not write a misleading audit record for a no-op.
        return target

    target.is_active = is_active
    target.save(update_fields=['is_active'])

    audit.record_user_action(
        actor=actor,
        action=AuditLog.ACTION_USER_ACTIVATED if is_active else AuditLog.ACTION_USER_DEACTIVATED,
        target_user=target,
        request=request,
        target_role=target.role,
    )
    return target


def delete_user(*, actor, target, request=None):
    """Hard-delete an account. Returns the summary of what went with it.

    The summary is computed BEFORE the delete (afterwards the rows are gone) and
    is recorded in the audit metadata, so the trail says what was destroyed and
    not merely that something was.
    """
    _assert_not_self(actor, target, "delete")
    _assert_not_last_admin(target, "delete")

    summary = deletion_summary(target)

    with transaction.atomic():
        # Recorded inside the transaction, before the cascade: the AuditLog row
        # holds no FK to the target precisely so it survives this.
        audit.record_user_action(
            actor=actor,
            action=AuditLog.ACTION_USER_DELETED,
            target_user=target,
            request=request,
            target_role=target.role,
            deleted=summary,
        )
        target.delete()

    return summary


def _assert_is_owner(target):
    if target.role != 'station_owner':
        raise ValidationError("Only station owners require approval.")


def approve_owner(*, actor, target, request=None):
    """Approve a station owner. Idempotent."""
    _assert_is_owner(target)
    previous = target.owner_status
    if previous == User.OWNER_APPROVED:
        return target

    target.owner_status = User.OWNER_APPROVED
    target.save(update_fields=['owner_status'])

    audit.record_user_action(
        actor=actor,
        action=AuditLog.ACTION_OWNER_APPROVED,
        target_user=target,
        request=request,
        previous_status=previous,
    )
    return target


def reject_owner(*, actor, target, reason=None, request=None):
    """Reject a station owner's application.

    The account stays active and usable as a customer — rejection withdraws
    station management, it is not a ban. Existing stations are deliberately left
    alone: see the B2 note in the sprint report.
    """
    _assert_is_owner(target)
    previous = target.owner_status
    if previous == User.OWNER_REJECTED:
        return target

    target.owner_status = User.OWNER_REJECTED
    target.save(update_fields=['owner_status'])

    audit.record_user_action(
        actor=actor,
        action=AuditLog.ACTION_OWNER_REJECTED,
        target_user=target,
        request=request,
        previous_status=previous,
        reason=reason or "",
    )
    return target
