import graphene
from graphene_file_upload.scalars import Upload
from graphene_django import DjangoObjectType
from .models import Favorite, Review, Station
from graphql_jwt.decorators import login_required
from accounts.permission import station_owner_required
from django.db.models import Avg
from django.db.models import Avg, Count, Exists, OuterRef, Value
from django.db.models.functions import Coalesce


class StationType(DjangoObjectType):
    num_of_reviews = graphene.Int()
    average_rating = graphene.Float()
    is_favorite = graphene.Boolean()
    class Meta:
        model = Station
        fields = '__all__'
        
    def resolve_num_of_reviews(self, info):
        return self.reviews.count()

    def resolve_average_rating(self, info):
        return self.reviews.aggregate(avg_rating=Avg("rating"))["avg_rating"] or 0.0
    
    def resolve_is_favorite(self, info):
        user = info.context.user
        if user.is_authenticated:
            return self.favorited_by.filter(user=user).exists()
        return False

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


class StationListType(graphene.ObjectType):
    station_id = graphene.ID()
    name = graphene.String()
    latitude = graphene.Float()
    longitude = graphene.Float()
    charger_type = graphene.String()
    num_of_charger = graphene.Int()
    num_of_rate = graphene.Int()
    average_rate = graphene.Float()
    is_favorite = graphene.Boolean()
    image = graphene.String()


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
            image=image 
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
        stationId = graphene.ID(required=True)

    @login_required
    @station_owner_required
    def mutate(self, info, stationId):
        user = info.context.user
        station = Station.objects.get(id=stationId, owner=user)
        station.delete()
        return DeleteStation(ok=True)

class CreateReview(graphene.Mutation):
    review = graphene.Field(lambda: ReviewType)

    class Arguments:
        station_id = graphene.ID(required=True)
        rating = graphene.Int(required=True)
        comment = graphene.String()

    @login_required
    def mutate(self, info, station_id, rating, comment=""):
        user = info.context.user
        station = Station.objects.get(id=station_id)

        # Optional: one review per user
        if Review.objects.filter(user=user, station=station).exists():
            raise Exception("You have already reviewed this station.")

        review = Review.objects.create(
            user=user,
            station=station,
            rating=rating,
            comment=comment
        )
        return CreateReview(review=review)
class ReviewType(DjangoObjectType):
    class Meta:
        model = Review
        fields = '__all__'

class ToggleFavoriteStation(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        station_id = graphene.ID(required=True)

    @login_required
    def mutate(self, info, station_id):
        user = info.context.user
        try:
            station = Station.objects.get(id=station_id)
        except Station.DoesNotExist:
            return ToggleFavoriteStation(success=False, message="Station not found")

        # Toggle logic
        favorite, created = Favorite.objects.get_or_create(user=user, station=station)
        if not created:
            favorite.delete()
            return ToggleFavoriteStation(success=True, message="Removed from favourites")
        return ToggleFavoriteStation(success=True, message="Added to favourites")


class StationMutation(graphene.ObjectType):
    create_station = CreateStation.Field()
    update_station = UpdateStation.Field()
    delete_station = DeleteStation.Field()
    create_review = CreateReview.Field()
    toggle_favorite_station = ToggleFavoriteStation.Field()


class StationQuery(graphene.ObjectType):
    station_list = graphene.List(StationListType)
    filter_stations = graphene.List(
        StationListType,
        charger_type=graphene.String(),
        num_of_charger=graphene.Int(),
        min_rating=graphene.Float(),
        min_power=graphene.Float(),
        max_power=graphene.Float()
    )
    station_by_id = graphene.Field(StationType, station_id=graphene.ID(required=True))

    def resolve_station_list(self, info):
        user = info.context.user

        stations = Station.objects.filter(is_active=True).annotate(
            num_of_rate=Count('reviews'),
            average_rate=Coalesce(Avg('reviews__rating'), 0.0)
        )

        if user.is_authenticated:
            favorite_subquery = Favorite.objects.filter(
                user=user,
                station=OuterRef('pk')
            )
            stations = stations.annotate(
                is_favorite=Exists(favorite_subquery)
            )
        else:
            stations = stations.annotate(
                is_favorite=Value(False)
            )

        return [
            StationListType(
                station_id=station.id,
                name=station.name,
                latitude=station.latitude,
                longitude=station.longitude,
                charger_type=station.charger_type,
                num_of_charger=station.num_of_charger,
                num_of_rate=station.num_of_rate,
                average_rate=round(station.average_rate, 1),
                is_favorite=getattr(station, 'is_favorite', False),
                image=station.image.url if station.image else ''
            )
            for station in stations
        ]

    def resolve_filter_stations(self, info, charger_type=None, num_of_charger=None,
                                min_rating=None, min_power=None, max_power=None):
        user = info.context.user

        stations = Station.objects.filter(is_active=True).annotate(
            num_of_rate=Count('reviews'),
            average_rate=Coalesce(Avg('reviews__rating'), 0.0)
        )

        if user.is_authenticated:
            favorite_subquery = Favorite.objects.filter(user=user, station=OuterRef('pk'))
            stations = stations.annotate(is_favorite=Exists(favorite_subquery))
        else:
            stations = stations.annotate(is_favorite=Value(False))

        if charger_type:
            stations = stations.filter(charger_type__icontains=charger_type)
        if num_of_charger:
            stations = stations.filter(num_of_charger=num_of_charger)
        if min_rating is not None:
            stations = stations.filter(average_rate__gte=min_rating)
        if min_power is not None:
            stations = stations.filter(power_output_kw__gte=min_power)
        if max_power is not None:
            stations = stations.filter(power_output_kw__lte=max_power)

        return [
            StationListType(
                station_id=s.id,
                name=s.name,
                latitude=s.latitude,
                longitude=s.longitude,
                charger_type=s.charger_type,
                num_of_charger=s.num_of_charger,
                num_of_rate=s.num_of_rate,
                average_rate=round(s.average_rate, 1),
                is_favorite=getattr(s, 'is_favorite', False),
                image=s.image.url if s.image else ''
            )
            for s in stations
        ]

    def resolve_station_by_id(self, info, station_id):
        try:
            return Station.objects.get(id=station_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")