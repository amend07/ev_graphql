"""Audit service (Sprint B1, Phase 4).

The single way an administrative action is recorded. Resolvers call
:func:`record_user_action` and nothing else — no resolver builds an AuditLog
itself, so the shape of the trail is defined here rather than re-invented at each
call site.

Two logs, one call: the durable database record, plus the existing structured
stdout line (:mod:`accounts.auth_logging`) so admin actions finally appear in the
same stream as the auth events they sit alongside.

Recording never breaks the action it describes: a logging fault is reported to
stderr and swallowed. Losing the trail of a completed delete is bad; failing the
delete *after* the rows are gone, and leaving the caller thinking it did not
happen, is worse.
"""

import logging

from .auth_logging import log_event
from .models import AuditLog
from .ratelimit import get_client_ip

logger = logging.getLogger("accounts.audit")


def record(
    *,
    actor,
    action,
    target_type,
    target_id,
    target_label="",
    request=None,
    **metadata,
):
    """Write one audit record. Returns the row, or None if recording failed.

    ``metadata`` is free-form context (the old/new value, a reason, a deletion
    summary). It must never carry a credential: it lands in the database as JSON.
    """
    ip = get_client_ip(request) if request is not None else None
    if ip == "unknown":
        ip = None

    try:
        entry = AuditLog.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            actor_username=getattr(actor, "username", "") or "",
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            target_label=target_label or "",
            metadata=metadata or {},
            ip=ip,
        )
    except Exception:  # pragma: no cover - only on a database fault
        logger.exception("Failed to write audit record for %s", action)
        entry = None

    # Mirror to the security log stream. log_event scrubs sensitive keys.
    log_event(action, request=request, user=actor, target_id=str(target_id), **metadata)
    return entry


def record_user_action(*, actor, action, target_user, request=None, **metadata):
    """Record an action taken against a user.

    The target's identity is snapshotted here rather than referenced, because the
    caller may be about to delete it.
    """
    return record(
        actor=actor,
        action=action,
        target_type=AuditLog.TARGET_USER,
        target_id=target_user.pk,
        target_label=target_user.username,
        request=request,
        **metadata,
    )
