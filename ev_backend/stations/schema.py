import graphene
from graphene_django import DjangoObjectType
from graphql_jwt.decorators import login_required
from accounts.permission import station_owner_required  # We'll create this

from .models import Station

class StationType(DjangoObjectType):
    class Meta:
        model = Station
        fields = "__all__"

class CreateStation(graphene.Mutation):
    station = graphene.Field(StationType)

    class Arguments:
        name = graphene.String(required=True)
        location = graphene.String(required=True)
        latitude = graphene.Float(required=True)
        longitude = graphene.Float(required=True)
        available_slots = graphene.Int(required=True)

    @station_owner_required
    def mutate(self, info, name, location, latitude, longitude, available_slots):
        user = info.context.user

        station = Station.objects.create(
            owner=user,
            name=name,
            location=location,
            latitude=latitude,
            longitude=longitude,
            available_slots=available_slots
        )

        return CreateStation(station=station)

class StationMutation(graphene.ObjectType):
    create_station = CreateStation.Field()
