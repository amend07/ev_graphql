import graphene
from graphene_django import DjangoObjectType
from .models import Booking
from graphql_jwt.decorators import login_required
from stations.models import Station
from accounts.permission import station_owner_required  # you already use this

class BookingType(DjangoObjectType):
    class Meta:
        model = Booking
        fields = '__all__'

class CreateBooking(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        station_id = graphene.ID(required=True)
        start_time = graphene.DateTime(required=True)
        end_time = graphene.DateTime(required=True)

    @login_required
    def mutate(self, info, station_id, start_time, end_time):
        user = info.context.user
        try:
            station = Station.objects.get(pk=station_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")

        booking = Booking.objects.create(
            user=user,
            station=station,
            start_time=start_time,
            end_time=end_time,
            status='pending',
        )

        return CreateBooking(booking=booking)

class UpdateBookingStatus(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        booking_id = graphene.ID(required=True)
        status = graphene.String(required=True)  # expected: approved / rejected
        cancel_reason = graphene.String()

    @login_required
    @station_owner_required
    def mutate(self, info, booking_id, status, cancel_reason=None):
        user = info.context.user

        try:
            booking = Booking.objects.get(id=booking_id)
        except Booking.DoesNotExist:
            raise Exception("Booking not found")

        if booking.station.owner != user:
            raise Exception("You do not own this station")

        if booking.status not in ['pending']:
            raise Exception("Only pending bookings can be updated")

        if status not in ['approved', 'rejected']:
            raise Exception("Invalid status. Must be approved or rejected")

        booking.status = status
        if status == 'rejected' and cancel_reason:
            booking.cancel_reason = cancel_reason
        booking.save()

        return UpdateBookingStatus(booking=booking)

class CancelBooking(graphene.Mutation):
    booking = graphene.Field(BookingType)

    class Arguments:
        booking_id = graphene.ID(required=True)
        cancel_reason = graphene.String()

    @login_required
    def mutate(self, info, booking_id, cancel_reason=None):
        user = info.context.user

        try:
            booking = Booking.objects.get(id=booking_id)
        except Booking.DoesNotExist:
            raise Exception("Booking not found")

        if booking.user != user:
            raise Exception("You can only cancel your own bookings")

        if booking.status in ['cancelled', 'done']:
            raise Exception("Cannot cancel a completed or already cancelled booking")

        booking.status = 'cancelled'
        booking.cancel_reason = cancel_reason or "Cancelled by user"
        booking.save()

        return CancelBooking(booking=booking)

class BookingQuery(graphene.ObjectType):
    my_bookings = graphene.List(BookingType, status=graphene.String())
    station_bookings = graphene.List(
        BookingType,
        station_id=graphene.ID(required=True),
        status=graphene.String()
    )
    
    @login_required
    def resolve_my_bookings(self, info, status=None):
        user = info.context.user
        qs = Booking.objects.filter(user=user).order_by('-start_time')
        if status:
            qs = qs.filter(status=status)
        return qs

    @login_required
    @station_owner_required
    def resolve_station_bookings(self, info, station_id, status=None):
        user = info.context.user

        try:
            station = Station.objects.get(id=station_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")

        if station.owner != user:
            raise Exception("You do not own this station")

        qs = Booking.objects.filter(station=station).order_by('-start_time')
        if status:
            qs = qs.filter(status=status)
        return qs


class BookingMutation(graphene.ObjectType):
    create_booking = CreateBooking.Field()
    update_booking_status = UpdateBookingStatus.Field()
    cancel_booking = CancelBooking.Field()
