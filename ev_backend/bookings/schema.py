import graphene
from graphene_django import DjangoObjectType
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from graphql_jwt.decorators import login_required

from .models import Booking
from stations.models import Station
from accounts.permission import station_owner_required, active_required
from ev_backend.pagination import paginate, hard_cap, clamp_page_size, clamp_offset


class BookingType(DjangoObjectType):
    class Meta:
        model = Booking
        fields = "__all__"


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

    @login_required
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
        return CreateBooking(booking=booking)


class UpdateBookingStatus(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        booking_id = graphene.ID(required=True)
        status = graphene.String(required=True)
        cancel_reason = graphene.String()

    @login_required
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

        return UpdateBookingStatus(booking=booking)


class CancelBooking(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        booking_id = graphene.ID(required=True)
        cancel_reason = graphene.String()

    @login_required
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
        if limit is not None:
            start = clamp_offset(offset)
            return qs[start:start + clamp_page_size(limit)]
        return hard_cap(qs)

    @login_required
    def resolve_my_bookings_page(self, info, status=None, limit=None, offset=None):
        items, total, has_next = paginate(_my_bookings_qs(info.context.user, status), limit, offset)
        return BookingPage(items=items, total_count=total, has_next=has_next)

    @login_required
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
        if limit is not None:
            start = clamp_offset(offset)
            return qs[start:start + clamp_page_size(limit)]
        return hard_cap(qs)


class BookingMutation(graphene.ObjectType):
    create_booking = CreateBooking.Field()
    update_booking_status = UpdateBookingStatus.Field()
    cancel_booking = CancelBooking.Field()
