import graphene
from django.db import IntegrityError, transaction
from graphene_django import DjangoObjectType

from accounts.permission import active_required, login_required
from ev_backend.pagination import paginate
from stations.validators import InvalidInput, normalize_charger_type

from .models import Vehicle

# A car should be recent enough to be an EV but we stay lenient about the exact
# bounds — the point is to reject a typo'd 202 or 20255, not to police model years.
_MIN_YEAR = 1990
_MAX_YEAR = 2100
_MAX_BATTERY_KWH = 500


class VehicleType(DjangoObjectType):
    class Meta:
        model = Vehicle
        # Keep charger_type a plain String on the wire (not a generated enum), so
        # the canonical codes ("type2") pass through unchanged and both clients
        # can treat it as a string exactly like the station's charger_type.
        convert_choices_to_enum = False
        fields = (
            'id', 'make', 'model', 'year', 'battery_capacity_kwh',
            'charger_type', 'plate_number', 'is_primary', 'created_at',
        )


class VehiclePage(graphene.ObjectType):
    items = graphene.List(VehicleType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


class VehicleInput(graphene.InputObjectType):
    make = graphene.String()
    model = graphene.String()
    year = graphene.Int()
    battery_capacity_kwh = graphene.Float()
    charger_type = graphene.String()
    plate_number = graphene.String()
    is_primary = graphene.Boolean()


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _validate(data, *, partial):
    def has(key):
        return data.get(key) is not None

    if not partial or has('make'):
        if _blank(data.get('make')):
            raise InvalidInput("Company / make is required.")
    if not partial or has('model'):
        if _blank(data.get('model')):
            raise InvalidInput("Model is required.")
    if not partial or has('plate_number'):
        if _blank(data.get('plate_number')):
            raise InvalidInput("Plate number is required.")
    if not partial or has('year'):
        year = data.get('year')
        if year is None or not (_MIN_YEAR <= int(year) <= _MAX_YEAR):
            raise InvalidInput(f"Year must be between {_MIN_YEAR} and {_MAX_YEAR}.")
    if not partial or has('battery_capacity_kwh'):
        cap = data.get('battery_capacity_kwh')
        if cap is None or float(cap) <= 0:
            raise InvalidInput("Battery capacity must be greater than 0.")
        if float(cap) > _MAX_BATTERY_KWH:
            raise InvalidInput(f"Battery capacity must be at most {_MAX_BATTERY_KWH} kWh.")
    if not partial or has('charger_type'):
        normalized = normalize_charger_type(data.get('charger_type'))
        allowed = {code for code, _ in Vehicle._meta.get_field('charger_type').choices}
        if normalized not in allowed:
            raise InvalidInput(
                f"Invalid charger type. Allowed: {', '.join(sorted(allowed))}."
            )


_FIELDS = ('make', 'model', 'year', 'battery_capacity_kwh',
           'charger_type', 'plate_number')


def _input_to_data(input):
    data = {f: getattr(input, f) for f in _FIELDS if getattr(input, f, None) is not None}
    if 'charger_type' in data:
        data['charger_type'] = normalize_charger_type(data['charger_type'])
    return data


def _clear_other_primaries(owner, keep_id=None):
    qs = Vehicle.objects.filter(owner=owner, is_primary=True)
    if keep_id is not None:
        qs = qs.exclude(id=keep_id)
    qs.update(is_primary=False)


class VehicleQuery(graphene.ObjectType):
    my_vehicles_page = graphene.Field(
        VehiclePage, limit=graphene.Int(), offset=graphene.Int(),
    )

    @login_required
    def resolve_my_vehicles_page(self, info, limit=None, offset=None):
        qs = Vehicle.objects.filter(owner=info.context.user)
        items, total, has_next = paginate(qs, limit, offset)
        return VehiclePage(items=items, total_count=total, has_next=has_next)


class AddVehicle(graphene.Mutation):
    vehicle = graphene.Field(VehicleType)

    class Arguments:
        input = VehicleInput(required=True)

    @active_required
    def mutate(self, info, input):
        user = info.context.user
        data = _input_to_data(input)
        try:
            _validate(data, partial=False)
        except InvalidInput as e:
            raise Exception(str(e))

        # The first car is primary by default; otherwise honour the request.
        first_vehicle = not Vehicle.objects.filter(owner=user).exists()
        make_primary = bool(input.is_primary) or first_vehicle

        try:
            with transaction.atomic():
                if make_primary:
                    _clear_other_primaries(user)
                vehicle = Vehicle.objects.create(
                    owner=user, is_primary=make_primary, **data,
                )
        except IntegrityError:
            raise Exception("You already have a vehicle with this plate number.")
        return AddVehicle(vehicle=vehicle)


class UpdateVehicle(graphene.Mutation):
    vehicle = graphene.Field(VehicleType)

    class Arguments:
        vehicle_id = graphene.ID(required=True)
        input = VehicleInput(required=True)

    @active_required
    def mutate(self, info, vehicle_id, input):
        user = info.context.user
        try:
            vehicle = Vehicle.objects.get(id=vehicle_id, owner=user)
        except Vehicle.DoesNotExist:
            raise Exception("Vehicle not found.")

        data = _input_to_data(input)
        try:
            _validate(data, partial=True)
        except InvalidInput as e:
            raise Exception(str(e))

        try:
            with transaction.atomic():
                for field, value in data.items():
                    setattr(vehicle, field, value)
                # A vehicle may be promoted to primary here, but never demoted to
                # non-primary by an update — clearing the everyday car is done by
                # promoting another one, so there is always exactly one.
                if input.is_primary:
                    _clear_other_primaries(user, keep_id=vehicle.id)
                    vehicle.is_primary = True
                vehicle.save()
        except IntegrityError:
            raise Exception("You already have a vehicle with this plate number.")
        return UpdateVehicle(vehicle=vehicle)


class SetPrimaryVehicle(graphene.Mutation):
    vehicle = graphene.Field(VehicleType)

    class Arguments:
        vehicle_id = graphene.ID(required=True)

    @active_required
    def mutate(self, info, vehicle_id):
        user = info.context.user
        try:
            vehicle = Vehicle.objects.get(id=vehicle_id, owner=user)
        except Vehicle.DoesNotExist:
            raise Exception("Vehicle not found.")
        with transaction.atomic():
            _clear_other_primaries(user, keep_id=vehicle.id)
            if not vehicle.is_primary:
                vehicle.is_primary = True
                vehicle.save(update_fields=['is_primary'])
        return SetPrimaryVehicle(vehicle=vehicle)


class DeleteVehicle(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        vehicle_id = graphene.ID(required=True)

    @active_required
    def mutate(self, info, vehicle_id):
        user = info.context.user
        try:
            vehicle = Vehicle.objects.get(id=vehicle_id, owner=user)
        except Vehicle.DoesNotExist:
            raise Exception("Vehicle not found.")
        vehicle.delete()
        return DeleteVehicle(ok=True)


class VehicleMutation(graphene.ObjectType):
    add_vehicle = AddVehicle.Field()
    update_vehicle = UpdateVehicle.Field()
    set_primary_vehicle = SetPrimaryVehicle.Field()
    delete_vehicle = DeleteVehicle.Field()
