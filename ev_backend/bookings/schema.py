import graphene
from graphene_django import DjangoObjectType
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta


from .models import Booking
from stations.models import Station
from accounts.permission import (
    active_required,
    admin_required,
    login_required,
    station_owner_required,
)
from ev_backend.errors import NotFound
from ev_backend.pagination import apply_ordering, paginate, window
from notifications.models import Notification
from notifications.service import notify


class BookingCustomerType(graphene.ObjectType):
    """The customer on a booking, as the counterparty may see them.

    Carries the email, which `PublicUserType` does not: a station owner has to be
    able to contact the driver they just approved, and the customer sees their
    own. Both clients select exactly these three fields.

    Safe only because every route to a booking is authorization-gated —
    `myBookings*` is self-scoped, `stationBookings` is owner-scoped, and B1
    removed `Station.bookings`, which was the anonymous way in.
    """

    id = graphene.ID(required=True)
    username = graphene.String(required=True)
    email = graphene.String(required=True)


class BookingType(DjangoObjectType):
    # required: Booking.user is a non-null FK (was `UserType!`).
    user = graphene.Field(BookingCustomerType, required=True)

    class Meta:
        model = Booking
        # Explicit allow-list (B1 Phase 1) — see StationType.
        fields = ("id", "start_time", "end_time", "status", "cancel_reason",
                  "created_at", "station")
        # Return `status` as its raw lowercase value ("pending", "approved", …)
        # instead of graphene-django's auto-generated UPPERCASE choice enum.
        # The mobile client and these tests depend on the lowercase contract.
        convert_choices_to_enum = False

    def resolve_user(self, info):
        return BookingCustomerType(
            id=self.user.id, username=self.user.username, email=self.user.email,
        )


class BookingPage(graphene.ObjectType):
    items = graphene.List(BookingType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


def _my_bookings_qs(user, status=None):
    qs = (
        Booking.objects.filter(user=user)
        .select_related("station", "station__owner")
        .order_by("-start_time")
    )
    if status:
        qs = qs.filter(status=status)
    return qs


class CreateBooking(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        station_id = graphene.ID(required=True)
        start_time = graphene.DateTime(required=True)
        end_time = graphene.DateTime(required=True)

    @active_required
    def mutate(self, info, station_id, start_time, end_time):
        user = info.context.user
        now = timezone.now()

        # --- time validation (independent of the station row) ---
        if start_time <= now:
            raise Exception("Start time must be in the future")
        if end_time <= start_time:
            raise Exception("End time must be after start time")

        duration = end_time - start_time
        min_minutes = settings.BOOKING_MIN_DURATION_MINUTES
        max_hours = settings.BOOKING_MAX_DURATION_HOURS
        if duration < timedelta(minutes=min_minutes):
            raise Exception(f"Minimum booking duration is {min_minutes} minutes")
        if duration > timedelta(hours=max_hours):
            raise Exception(f"Maximum booking duration is {max_hours} hours")

        # --- station + availability, serialised per station to avoid races ---
        with transaction.atomic():
            try:
                station = Station.objects.select_for_update().get(pk=station_id)
            except Station.DoesNotExist:
                raise Exception("Station not found")

            if not station.is_active:
                raise Exception("This station is not available for booking")

            if station.owner_id == user.id and not settings.BOOKING_ALLOW_OWNER_SELF_BOOKING:
                raise Exception("You cannot book your own station")

            overlapping = Booking.objects.filter(
                station=station,
                status__in=Booking.ACTIVE_STATUSES,
                start_time__lt=end_time,
                end_time__gt=start_time,
            ).count()
            if overlapping >= station.num_of_charger:
                raise Exception("No chargers are available for the selected time slot")

            booking = Booking.objects.create(
                user=user,
                station=station,
                start_time=start_time,
                end_time=end_time,
                status="pending",
            )

            # Tell the owner a slot was requested. Skipped for an owner booking
            # their own station (no self-notification).
            if station.owner_id != user.id:
                notify(
                    recipient=station.owner_id,
                    notification_type=Notification.TYPE_BOOKING,
                    title="New booking request",
                    body=f"{user.username} requested a booking at {station.name}.",
                    related_id=booking.id,
                )
        return CreateBooking(booking=booking)


class UpdateBookingStatus(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        booking_id = graphene.ID(required=True)
        status = graphene.String(required=True)
        cancel_reason = graphene.String()

    @station_owner_required
    def mutate(self, info, booking_id, status, cancel_reason=None):
        user = info.context.user

        # Owners may approve/reject a pending booking or mark an approved
        # booking as completed.
        if status not in ("approved", "rejected", "done"):
            raise Exception("Invalid status. Must be approved, rejected, or done")

        with transaction.atomic():
            try:
                booking = (
                    Booking.objects.select_for_update()
                    .select_related("station")
                    .get(id=booking_id)
                )
            except Booking.DoesNotExist:
                raise Exception("Booking not found")

            if booking.station.owner_id != user.id:
                raise Exception("You do not own this station")

            if not booking.can_transition_to(status):
                raise Exception(f"Cannot change a {booking.status} booking to {status}")

            booking.status = status
            if status == "rejected" and cancel_reason:
                booking.cancel_reason = cancel_reason
            booking.save(update_fields=["status", "cancel_reason"])

            # Let the customer know their booking moved.
            outcome = {"approved": "approved", "rejected": "rejected", "done": "completed"}[status]
            notify(
                recipient=booking.user_id,
                notification_type=Notification.TYPE_BOOKING,
                title=f"Booking {outcome}",
                body=f"Your booking at {booking.station.name} was {outcome}.",
                related_id=booking.id,
            )

        return UpdateBookingStatus(booking=booking)


class CancelBooking(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        booking_id = graphene.ID(required=True)
        cancel_reason = graphene.String()

    @active_required
    def mutate(self, info, booking_id, cancel_reason=None):
        user = info.context.user

        with transaction.atomic():
            try:
                booking = Booking.objects.select_for_update().get(id=booking_id)
            except Booking.DoesNotExist:
                raise Exception("Booking not found")

            if booking.user_id != user.id:
                raise Exception("You can only cancel your own bookings")

            if not booking.can_transition_to("cancelled"):
                raise Exception("Cannot cancel a completed or already cancelled booking")

            booking.status = "cancelled"
            booking.cancel_reason = cancel_reason or "Cancelled by user"
            booking.save(update_fields=["status", "cancel_reason"])

            # Let the owner know a slot freed up. (station is not select_related
            # here, so owner_id costs one extra query — acceptable for a cancel.)
            if booking.station.owner_id != user.id:
                notify(
                    recipient=booking.station.owner_id,
                    notification_type=Notification.TYPE_BOOKING,
                    title="Booking cancelled",
                    body=f"{user.username} cancelled their booking at {booking.station.name}.",
                    related_id=booking.id,
                )

        return CancelBooking(booking=booking)


class BookingQuery(graphene.ObjectType):
    my_bookings = graphene.List(
        BookingType, status=graphene.String(),
        limit=graphene.Int(), offset=graphene.Int(),
    )
    station_bookings = graphene.List(
        BookingType,
        booking_id=graphene.ID(required=True),  # station id (kept for API compatibility)
        status=graphene.String(),
        limit=graphene.Int(), offset=graphene.Int(),
    )
    # Proper paginated endpoint with a total count (Part 2).
    my_bookings_page = graphene.Field(
        BookingPage, status=graphene.String(),
        limit=graphene.Int(), offset=graphene.Int(),
    )

    @login_required
    def resolve_my_bookings(self, info, status=None, limit=None, offset=None):
        qs = _my_bookings_qs(info.context.user, status)
        return window(qs, limit, offset)

    @login_required
    def resolve_my_bookings_page(self, info, status=None, limit=None, offset=None):
        items, total, has_next = paginate(_my_bookings_qs(info.context.user, status), limit, offset)
        return BookingPage(items=items, total_count=total, has_next=has_next)

    @station_owner_required
    def resolve_station_bookings(self, info, booking_id, status=None, limit=None, offset=None):
        user = info.context.user
        try:
            station = Station.objects.get(id=booking_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")

        if station.owner_id != user.id:
            raise Exception("You do not own this station")

        qs = (
            Booking.objects.filter(station=station)
            .select_related("user", "station")
            .order_by("-start_time")
        )
        if status:
            qs = qs.filter(status=status)
        return window(qs, limit, offset)


class BookingMutation(graphene.ObjectType):
    create_booking = CreateBooking.Field()
    update_booking_status = UpdateBookingStatus.Field()
    cancel_booking = CancelBooking.Field()


# ── Administration (Sprint B2.1) ─────────────────────────────────────────────
#
# Read-only, deliberately and completely. There is no admin status override, no
# forced cancellation and no refund. `Booking` carries no money, no price
# snapshot and no payment reference, so a refund mutation here could not do
# anything except lie about having issued one. See the sprint report.

BOOKING_ORDER_FIELDS = {
    'newest': '-created_at',
    'oldest': 'created_at',
    'start_time': '-start_time',
    'status': 'status',
}


class BookingAdminQuery(graphene.ObjectType):
    bookings_page = graphene.Field(
        BookingPage,
        status=graphene.String(),
        customer_id=graphene.ID(),
        owner_id=graphene.ID(),
        station_id=graphene.ID(),
        date_from=graphene.DateTime(),
        date_to=graphene.DateTime(),
        search=graphene.String(),
        order_by=graphene.String(),
        limit=graphene.Int(),
        offset=graphene.Int(),
        description=(
            "Every booking on the platform, read-only. Admin only. "
            "dateFrom/dateTo filter on startTime — the booking's slot, not when "
            "it was created. search matches customer username/email or station name."
        ),
    )
    booking_by_id = graphene.Field(
        BookingType,
        booking_id=graphene.ID(required=True),
        description="One booking, read-only. Admin only.",
    )

    @admin_required
    def resolve_bookings_page(self, info, status=None, customer_id=None,
                              owner_id=None, station_id=None, date_from=None,
                              date_to=None, search=None, order_by=None,
                              limit=None, offset=None):
        qs = Booking.objects.select_related("user", "station", "station__owner")

        if status:
            qs = qs.filter(status=status)
        if customer_id:
            qs = qs.filter(user_id=customer_id)
        if owner_id:
            qs = qs.filter(station__owner_id=owner_id)
        if station_id:
            qs = qs.filter(station_id=station_id)
        # Filters the slot, not the creation timestamp: an admin asking for
        # "bookings this week" on a bookings screen means the ones happening
        # then. dashboardSummary counts the opposite (bookings *made*). Both are
        # documented rather than left for the caller to infer.
        if date_from is not None:
            qs = qs.filter(start_time__gte=date_from)
        if date_to is not None:
            qs = qs.filter(start_time__lte=date_to)
        if search:
            term = search.strip()
            if term:
                qs = qs.filter(
                    Q(user__username__icontains=term)
                    | Q(user__email__icontains=term)
                    | Q(station__name__icontains=term)
                )

        qs = apply_ordering(qs, order_by, BOOKING_ORDER_FIELDS, '-created_at')

        items, total, has_next = paginate(qs, limit, offset)
        return BookingPage(items=items, total_count=total, has_next=has_next)

    @admin_required
    def resolve_booking_by_id(self, info, booking_id):
        try:
            return (
                Booking.objects
                .select_related("user", "station", "station__owner")
                .get(pk=booking_id)
            )
        except (Booking.DoesNotExist, ValueError, TypeError):
            raise NotFound("Booking not found.")
