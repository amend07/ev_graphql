"""Notification service.

Resolvers and domain services call :func:`notify` / :func:`notify_admins` rather
than constructing ``Notification`` rows directly — the same seam
``accounts.audit`` uses. Keeping creation in one place means a later push/email
channel is a change here, not at every call site.
"""

from django.contrib.auth import get_user_model

from .models import Notification, NotificationPreference

User = get_user_model()


def _pref_allows(pref, notification_type):
    """A missing preference row means all-on (see NotificationPreference)."""
    return True if pref is None else pref.allows(notification_type)


def notify(*, recipient, notification_type, title, body='', related_id=''):
    """Create a single notification, unless the recipient has opted out of this
    category. ``recipient`` may be a User or a user id. Returns the row, or
    ``None`` if the recipient's preferences suppress it."""
    recipient_id = getattr(recipient, 'id', recipient)
    pref = NotificationPreference.objects.filter(user_id=recipient_id).first()
    if not _pref_allows(pref, notification_type):
        return None
    return Notification.objects.create(
        recipient_id=recipient_id,
        notification_type=notification_type,
        title=title,
        body=body or '',
        related_id=str(related_id or ''),
    )


def notify_admins(*, notification_type, title, body='', related_id='', exclude_id=None):
    """Fan a notification out to every active admin (e.g. a new owner awaiting
    approval), skipping any admin who has opted out of this category. Returns the
    created rows."""
    admins = User.objects.filter(role='admin', is_active=True)
    if exclude_id is not None:
        admins = admins.exclude(id=exclude_id)
    prefs = {
        p.user_id: p
        for p in NotificationPreference.objects.filter(user__in=admins)
    }
    return Notification.objects.bulk_create([
        Notification(
            recipient=admin,
            notification_type=notification_type,
            title=title,
            body=body or '',
            related_id=str(related_id or ''),
        )
        for admin in admins
        if _pref_allows(prefs.get(admin.id), notification_type)
    ])
