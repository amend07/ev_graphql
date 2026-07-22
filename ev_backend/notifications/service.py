"""Notification service.

Resolvers and domain services call :func:`notify` / :func:`notify_admins` rather
than constructing ``Notification`` rows directly — the same seam
``accounts.audit`` uses. Keeping creation in one place means a later push/email
channel is a change here, not at every call site.
"""

from django.contrib.auth import get_user_model

from .models import Notification

User = get_user_model()


def notify(*, recipient, notification_type, title, body='', related_id=''):
    """Create a single notification. ``recipient`` may be a User or a user id."""
    recipient_id = getattr(recipient, 'id', recipient)
    return Notification.objects.create(
        recipient_id=recipient_id,
        notification_type=notification_type,
        title=title,
        body=body or '',
        related_id=str(related_id or ''),
    )


def notify_admins(*, notification_type, title, body='', related_id='', exclude_id=None):
    """Fan a notification out to every active admin (e.g. a new owner awaiting
    approval). Returns the created rows."""
    admins = User.objects.filter(role='admin', is_active=True)
    if exclude_id is not None:
        admins = admins.exclude(id=exclude_id)
    return Notification.objects.bulk_create([
        Notification(
            recipient=admin,
            notification_type=notification_type,
            title=title,
            body=body or '',
            related_id=str(related_id or ''),
        )
        for admin in admins
    ])
