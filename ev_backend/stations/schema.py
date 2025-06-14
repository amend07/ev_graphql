import graphene
from graphene_file_upload.scalars import Upload
from graphene_django import DjangoObjectType
from .models import Station
from graphql_jwt.decorators import login_required
from accounts.permission import station_owner_required

class StationType(DjangoObjectType):
    class Meta:
        model = Station
        fields = '__all__'

class CreateStationInput(graphene.InputObjectType):
    name = graphene.String(required=True)
    contact_info = graphene.String()
    description = graphene.String()
    location = graphene.String(required=True)
    latitude = graphene.Float(required=True)
    longitude = graphene.Float(required=True)
    availability = graphene.String()
    amenities = graphene.String()
    charger_type = graphene.String()
    station_count = graphene.Int()
    num_of_charger = graphene.Int()
    power_output_kw = graphene.Float()
    estimated_time_min = graphene.Int()
    price_per_kwh = graphene.Float()
    charger_brand = graphene.String()

class CreateStation(graphene.Mutation):
    station = graphene.Field(StationType)

    class Arguments:
        input = CreateStationInput(required=True)
        image = Upload(required=False)

    @login_required
    @station_owner_required
    def mutate(self, info, input, image=None):
        user = info.context.user
        station = Station.objects.create(
            owner=user,
            name=input.name,
            contact_info=input.contact_info,
            description=input.description,
            location=input.location,
            latitude=input.latitude,
            longitude=input.longitude,
            availability=input.availability,
            amenities=input.amenities,
            charger_type=input.charger_type,
            station_count=input.station_count,
            num_of_charger=input.num_of_charger,
            power_output_kw=input.power_output_kw,
            estimated_time_min=input.estimated_time_min,
            price_per_kwh=input.price_per_kwh,
            charger_brand=input.charger_brand,
            image=image  # 👈 handle uploaded file
        )
        return CreateStation(station=station)

class UpdateStation(graphene.Mutation):
    station = graphene.Field(StationType)

    class Arguments:
        station_id = graphene.ID(required=True)
        input = CreateStationInput(required=False)
        image = Upload(required=False)
    @login_required
    @station_owner_required
    def mutate(self, info, station_id, input=None, image=None):
        user = info.context.user

        try:
            station = Station.objects.get(id=station_id, owner=user)
        except Station.DoesNotExist:
            raise Exception("Station not found or not owned by you.")

        # Update fields from input if provided
        if input:
            for field, value in input.items():
                setattr(station, field, value)

        # Update image if provided
        if image:
            station.image = image

        station.save()
        return UpdateStation(station=station)

class DeleteStation(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        id = graphene.ID(required=True)

    @login_required
    @station_owner_required
    def mutate(self, info, id):
        user = info.context.user
        station = Station.objects.get(id=id, owner=user)
        station.delete()
        return DeleteStation(ok=True)

class StationMutation(graphene.ObjectType):
    create_station = CreateStation.Field()
    update_station = UpdateStation.Field()
    delete_station = DeleteStation.Field()
class StationQuery(graphene.ObjectType):
    all_stations = graphene.List(StationType)

    def resolve_all_stations(root, info):
        return Station.objects.filter(is_active=True)