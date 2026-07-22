import graphene
from graphene_django import DjangoObjectType

from accounts.permission import login_required
from ev_backend.pagination import paginate

from .models import Notification


class NotificationType(DjangoObjectType):
    # `type` mirrors the client entity's field name; `related_id` is exposed
    # explicitly so a deep-link target is available without leaking the recipient.
    type = graphene.String()
    related_id = graphene.String()

    class Meta:
        model = Notification
        fields = ('id', 'title', 'body', 'is_read', 'created_at')

    def resolve_type(self, info):
        return self.notification_type

    def resolve_related_id(self, info):
        return self.related_id


class NotificationPage(graphene.ObjectType):
    items = graphene.List(NotificationType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


class NotificationQuery(graphene.ObjectType):
    my_notifications_page = graphene.Field(
        NotificationPage,
        limit=graphene.Int(),
        offset=graphene.Int(),
        unread_only=graphene.Boolean(),
    )
    my_unread_notification_count = graphene.Int()

    @login_required
    def resolve_my_notifications_page(
        self, info, limit=None, offset=None, unread_only=False
    ):
        qs = Notification.objects.filter(recipient=info.context.user)
        if unread_only:
            qs = qs.filter(is_read=False)
        items, total, has_next = paginate(qs, limit, offset)
        return NotificationPage(items=items, total_count=total, has_next=has_next)

    @login_required
    def resolve_my_unread_notification_count(self, info):
        return Notification.objects.filter(
            recipient=info.context.user, is_read=False
        ).count()


class MarkNotificationRead(graphene.Mutation):
    ok = graphene.Boolean()
    notification = graphene.Field(NotificationType)

    class Arguments:
        notification_id = graphene.ID(required=True)

    @login_required
    def mutate(self, info, notification_id):
        user = info.context.user
        try:
            notification = Notification.objects.get(
                id=notification_id, recipient=user
            )
        except Notification.DoesNotExist:
            raise Exception("Notification not found")
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=['is_read'])
        return MarkNotificationRead(ok=True, notification=notification)


class MarkAllNotificationsRead(graphene.Mutation):
    ok = graphene.Boolean()
    updated = graphene.Int()

    @login_required
    def mutate(self, info):
        updated = Notification.objects.filter(
            recipient=info.context.user, is_read=False
        ).update(is_read=True)
        return MarkAllNotificationsRead(ok=True, updated=updated)


class NotificationMutation(graphene.ObjectType):
    mark_notification_read = MarkNotificationRead.Field()
    mark_all_notifications_read = MarkAllNotificationsRead.Field()
