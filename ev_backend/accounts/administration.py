"""Administration service layer (Sprint B1, Phase 3).

Holds the business rules for administrative actions once, so the resolvers in
``schema.py`` stay thin — the same split ``services.py`` uses for credentials.

Everything destructive goes through here, which is what makes the safety
interlocks and the audit trail unskippable: a resolver cannot delete a user
without also recording it, because it does not do the deleting.
"""

from django.db import transaction
from django.db.models import Avg, Count, F, Q
from django.utils import timezone

from ev_backend.errors import Conflict, NotFound, ValidationError

from . import audit
from .models import AuditLog, User
from notifications.models import Notification
from notifications.service import notify


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

    Only ever triggers for an active admin. Admin privilege is not grantable over
    the API by design, so losing the last one costs server access to recover.
    """
    if target.role == 'admin' and target.is_active and _active_admin_count(excluding=target) == 0:
        raise Conflict(
            f"You cannot {action} the last active administrator. "
            "Promote another administrator first "
            "(on the server: manage.py promote_admin <username>)."
        )


def promote_to_admin(*, target, actor=None, via='', request=None):
    """Grant administrator privilege. Idempotent.

    Deliberately has NO GraphQL mutation in front of it, and that is the security
    decision of this sprint rather than an omission:

    * Today, stealing an admin session buys damage. If promotion were an API call
      it would also buy PERSISTENCE — the attacker mints a second admin, and
      revoking the one you noticed changes nothing. Keeping the grant off the
      network means recovery is always possible by an operator with server access.
    * The zero-admin bootstrap cannot be solved by a mutation anyway: you would
      need an admin to call it.
    * It is a rare, high-consequence operation. Needing a shell is a cost paid
      about once a year, and it is the whole safeguard.

    ``actor`` is None for a shell run — no one is signed in. ``via`` records what
    performed it, so the trail distinguishes an operator on the box from a UI
    action if a mutation is ever added.
    """
    if target.role == 'admin':
        # Idempotent: no state change, so no misleading audit record.
        return target

    previous = target.role
    target.role = 'admin'
    # Django's own admin site and its permission checks key off is_staff, not our
    # `role`. Granting one without the other produces an "admin" who fails half
    # the checks in the codebase.
    target.is_staff = True
    target.save(update_fields=['role', 'is_staff'])

    audit.record_user_action(
        actor=actor,
        action=AuditLog.ACTION_ADMIN_PROMOTED,
        target_user=target,
        request=request,
        previous_role=previous,
        via=via or 'unspecified',
    )
    return target


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


def soft_delete_own_account(*, user, request=None):
    """Self-service account deletion (W9) — a SOFT delete.

    Deliberately NOT `delete_user`: that one is an admin hard-cascading someone
    ELSE (and asserts the actor is not the target). Here the user deletes
    themselves and their data is KEPT so they can come back: the account is
    stamped ``deleted_at``, set inactive, and its sessions are invalidated
    (``token_version`` bumped). Signing in again restores it — see
    ``restore_account``.

    The last-admin guard stays — a sole admin removing themselves would leave the
    platform unadministrable — refused with the same message an admin-initiated
    attempt gets.
    """
    _assert_not_last_admin(user, "delete")

    with transaction.atomic():
        audit.record_user_action(
            actor=user,
            action=AuditLog.ACTION_USER_DELETED,
            target_user=user,
            request=request,
            target_role=user.role,
            self_service=True,
            soft=True,
        )
        user.deleted_at = timezone.now()
        user.is_active = False
        user.token_version = F('token_version') + 1
        user.save(update_fields=['deleted_at', 'is_active', 'token_version'])
        user.refresh_from_db(fields=['token_version'])

    return user


def restore_account(user, request=None):
    """Reverse a soft delete on a successful sign-in. No-op if not deleted.

    Returns True if it actually restored, so the caller can log the event. The
    account's data was never removed, so restoring is just clearing the stamp and
    reactivating — the user resumes exactly where they left off."""
    if user.deleted_at is None:
        return False
    user.deleted_at = None
    user.is_active = True
    user.save(update_fields=['deleted_at', 'is_active'])
    audit.record_user_action(
        actor=user,
        action=AuditLog.ACTION_USER_ACTIVATED,
        target_user=user,
        request=request,
        target_role=user.role,
        self_service=True,
        restored=True,
    )
    return True


def _period_starts(now=None):
    """Local midnight today, start of this week (Monday), start of this month.

    Computed in the project's timezone, not UTC: "bookings today" on an admin
    dashboard means the operator's today. Returned as aware datetimes so the
    comparison is unambiguous whatever the database stores.
    """
    now = timezone.localtime(now or timezone.now())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        'today': today,
        'week': today - timezone.timedelta(days=today.weekday()),  # Monday
        'month': today.replace(day=1),
    }


def platform_summary(*, newest_limit=5):
    """Every number on the admin dashboard, in four aggregate queries.

    Deliberately computed by the database. Counting rows in Python means fetching
    every user, station and booking on the platform into memory to answer a
    question SQL answers with a scan — it works on seed data and falls over on
    real data, which is the worst possible failure shape for a page whose only
    job is to say how much real data there is.

    Two definitions worth stating, because both were choices and neither is
    self-evident from the field name:

    * The booking windows count bookings **created** in the period, not bookings
      **starting** in it. This is "how much did the platform take today", which
      is what the count sits next to on a dashboard. A tomorrow-scheduled booking
      made today counts today; today's arrival booked last week does not.
    * ``average_rating`` and ``reviews`` count only visible reviews, so a
      moderated review stops affecting the platform-wide number exactly as it
      stops affecting the station's.

    There is deliberately no revenue, utilisation or growth figure here: the
    Booking model carries no money and no price snapshot, so every one of them
    would have to be invented. See the sprint report.
    """
    from bookings.models import Booking
    from stations.models import Review, Station

    starts = _period_starts()
    limit = max(1, min(int(newest_limit or 5), 20))

    users = User.objects.aggregate(
        customers=Count('id', filter=Q(role='user')),
        owners=Count('id', filter=Q(role='station_owner')),
        pending_owners=Count(
            'id', filter=Q(role='station_owner', owner_status=User.OWNER_PENDING),
        ),
        rejected_owners=Count(
            'id', filter=Q(role='station_owner', owner_status=User.OWNER_REJECTED),
        ),
    )
    stations = Station.objects.aggregate(
        stations=Count('id'),
        active_stations=Count('id', filter=Q(is_active=True)),
        inactive_stations=Count('id', filter=Q(is_active=False)),
    )
    bookings = Booking.objects.aggregate(
        bookings_today=Count('id', filter=Q(created_at__gte=starts['today'])),
        bookings_this_week=Count('id', filter=Q(created_at__gte=starts['week'])),
        bookings_this_month=Count('id', filter=Q(created_at__gte=starts['month'])),
    )
    reviews = Review.visible().aggregate(
        reviews=Count('id'),
        average_rating=Avg('rating'),
    )

    return {
        **users,
        **stations,
        **bookings,
        'reviews': reviews['reviews'],
        # No reviews is 0.0, not null: the clients render this as a number.
        'average_rating': round(reviews['average_rating'], 2) if reviews['average_rating'] else 0.0,
        'newest_users': list(User.objects.order_by('-date_joined', '-id')[:limit]),
        'newest_stations': list(
            Station.objects.select_related('owner').order_by('-created_at', '-id')[:limit]
        ),
    }


def _assert_is_owner(target):
    if target.role != 'station_owner':
        raise ValidationError("Only station owners require approval.")


def approve_owner(*, actor, target, request=None):
    """Approve a station owner. Idempotent.

    Records who decided and when (B2.1). A prior rejection reason is cleared:
    leaving it would put `owner_status='approved'` next to "rejected: documents
    could not be verified", and the next reader has to guess which one is
    current. The audit trail keeps the history; the record carries the outcome.
    """
    _assert_is_owner(target)
    previous = target.owner_status
    if previous == User.OWNER_APPROVED:
        return target

    target.owner_status = User.OWNER_APPROVED
    target.reviewer = actor
    target.reviewed_at = timezone.now()
    target.rejection_reason = ''
    target.save(update_fields=[
        'owner_status', 'reviewer', 'reviewed_at', 'rejection_reason',
    ])

    notify(
        recipient=target,
        notification_type=Notification.TYPE_SYSTEM,
        title="You're approved as a station owner",
        body="Your station owner account has been approved. "
             "You can now add and manage stations.",
    )

    audit.record_user_action(
        actor=actor,
        action=AuditLog.ACTION_OWNER_APPROVED,
        target_user=target,
        request=request,
        previous_status=previous,
    )
    return target


def reject_owner(*, actor, target, reason, request=None):
    """Reject a station owner's application. A reason is mandatory.

    The account stays active and usable as a customer — rejection withdraws
    station management, it is not a ban. Existing stations are deliberately left
    alone: that is still an open product decision, tracked in the known
    deviations table in BACKEND_ENGINEERING_PRINCIPLES.md.

    ``reason`` is required rather than optional because it is the only thing the
    rejected owner can be told, and the only thing a second admin reopening the
    case can read. B1 accepted ``reason=None`` and recorded "", which produced
    rejections nobody could account for afterwards. Validated here in the service
    rather than at the GraphQL edge, so it holds for every caller.
    """
    _assert_is_owner(target)

    reason = (reason or '').strip()
    if not reason:
        raise ValidationError("A reason is required to reject a station owner.")

    previous = target.owner_status
    if previous == User.OWNER_REJECTED:
        return target

    target.owner_status = User.OWNER_REJECTED
    target.reviewer = actor
    target.reviewed_at = timezone.now()
    target.rejection_reason = reason
    target.save(update_fields=[
        'owner_status', 'reviewer', 'reviewed_at', 'rejection_reason',
    ])

    notify(
        recipient=target,
        notification_type=Notification.TYPE_SYSTEM,
        title="Station owner application declined",
        body=f"Your station owner application was not approved: {reason}",
    )

    audit.record_user_action(
        actor=actor,
        action=AuditLog.ACTION_OWNER_REJECTED,
        target_user=target,
        request=request,
        previous_status=previous,
        reason=reason,
    )
    return target
