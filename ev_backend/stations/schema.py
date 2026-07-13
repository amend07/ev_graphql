import graphene
from graphene_file_upload.scalars import Upload
from graphene_django import DjangoObjectType
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Avg, Count, Exists, OuterRef, Value
from django.db.models.functions import Coalesce
from graphql_jwt.decorators import login_required

from .models import Favorite, Review, Station
from .validators import (
    InvalidInput,
    validate_station_input,
    validate_rating,
    validate_comment,
)
from .cache import get_public_station_rows
from accounts.permission import station_owner_required, active_required
from ev_backend.pagination import paginate, hard_cap, clamp_page_size, clamp_offset

# Row keys shared between the cached station list and StationListType.
_ROW_KEYS = (
    "station_id", "name", "latitude", "longitude", "charger_type",
    "num_of_charger", "num_of_rate", "average_rate", "power_output_kw",
    "price_per_kwh", "image",
)

# Scalar Station fields accepted from input; used to build create/update payloads
# without ever passing an explicit None (which would clobber model defaults).
STATION_FIELDS = [
    "name", "contact_info", "description", "location", "latitude", "longitude",
    "availability", "amenities", "charger_type", "station_count", "num_of_charger",
    "power_output_kw", "estimated_time_min", "price_per_kwh", "charger_brand",
]


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
    power_output_kw = graphene.Float()
    price_per_kwh = graphene.String()
    is_favorite = graphene.Boolean()
    image = graphene.String()


class StationPage(graphene.ObjectType):
    items = graphene.List(StationListType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


class ReviewPage(graphene.ObjectType):
    items = graphene.List(lambda: ReviewType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


def _station_to_list_type(s, is_favorite=None):
    """Map an annotated Station instance to StationListType (avoids duplication)."""
    return StationListType(
        station_id=s.id,
        name=s.name,
        latitude=s.latitude,
        longitude=s.longitude,
        charger_type=s.charger_type,
        num_of_charger=s.num_of_charger,
        num_of_rate=getattr(s, "num_of_rate", 0),
        average_rate=round(getattr(s, "average_rate", 0.0), 1),
        power_output_kw=s.power_output_kw,
        price_per_kwh=str(s.price_per_kwh),
        is_favorite=getattr(s, "is_favorite", False) if is_favorite is None else is_favorite,
        image=s.image.url if s.image else "",
    )


def _input_to_data(input):
    """Provided (non-None) scalar fields only, so model defaults still apply."""
    return {f: getattr(input, f) for f in STATION_FIELDS if getattr(input, f, None) is not None}


class CreateStation(graphene.Mutation):
    station = graphene.Field(StationType)

    class Arguments:
        input = CreateStationInput(required=True)
        image = Upload(required=False)

    @login_required
    @station_owner_required
    def mutate(self, info, input, image=None):
        user = info.context.user
        data = _input_to_data(input)

        try:
            validate_station_input(data, partial=False)
        except InvalidInput as e:
            raise Exception(str(e))

        # Prevent obvious duplicates for the same owner.
        if Station.objects.filter(
            owner=user, name__iexact=data["name"],
            location__iexact=data["location"], is_active=True,
        ).exists():
            raise Exception("You already have a station with this name at this location.")

        station = Station.objects.create(owner=user, image=image, **data)
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

        if input:
            data = _input_to_data(input)
            try:
                validate_station_input(data, partial=True)
            except InvalidInput as e:
                raise Exception(str(e))
            for field, value in data.items():
                setattr(station, field, value)

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
        try:
            station = Station.objects.get(id=stationId, owner=user)
        except Station.DoesNotExist:
            raise Exception("Station not found or not owned by you.")
        # Soft delete: preserve booking/review history and hide from listings.
        if station.is_active:
            station.is_active = False
            station.save(update_fields=["is_active"])
        return DeleteStation(ok=True)


class ReviewType(DjangoObjectType):
    class Meta:
        model = Review
        fields = '__all__'


class CreateReview(graphene.Mutation):
    review = graphene.Field(lambda: ReviewType)

    class Arguments:
        station_id = graphene.ID(required=True)
        rating = graphene.Int(required=True)
        comment = graphene.String()

    @login_required
    @active_required
    def mutate(self, info, station_id, rating, comment=""):
        user = info.context.user

        try:
            validate_rating(rating)
            validate_comment(comment)
        except InvalidInput as e:
            raise Exception(str(e))

        try:
            station = Station.objects.get(id=station_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")

        if not station.is_active:
            raise Exception("This station is not available for review.")
        if station.owner_id == user.id:
            raise Exception("You cannot review your own station.")

        if settings.REVIEW_REQUIRE_COMPLETED_BOOKING:
            from bookings.models import Booking
            if not Booking.objects.filter(user=user, station=station, status="done").exists():
                raise Exception("You can only review a station after completing a booking there.")

        if Review.objects.filter(user=user, station=station).exists():
            raise Exception("You have already reviewed this station.")

        try:
            review = Review.objects.create(
                user=user, station=station, rating=rating, comment=comment or "",
            )
        except IntegrityError:
            # Unique constraint lost a race — surface the same clean message.
            raise Exception("You have already reviewed this station.")
        return CreateReview(review=review)


class UpdateReview(graphene.Mutation):
    review = graphene.Field(lambda: ReviewType)

    class Arguments:
        review_id = graphene.ID(required=True)
        rating = graphene.Int()
        comment = graphene.String()

    @login_required
    @active_required
    def mutate(self, info, review_id, rating=None, comment=None):
        user = info.context.user
        try:
            review = Review.objects.get(id=review_id)
        except Review.DoesNotExist:
            raise Exception("Review not found")

        if review.user_id != user.id:
            raise Exception("You can only edit your own review.")

        try:
            if rating is not None:
                validate_rating(rating)
                review.rating = rating
            if comment is not None:
                validate_comment(comment)
                review.comment = comment
        except InvalidInput as e:
            raise Exception(str(e))

        review.save()
        return UpdateReview(review=review)


class DeleteReview(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        review_id = graphene.ID(required=True)

    @login_required
    @active_required
    def mutate(self, info, review_id):
        user = info.context.user
        try:
            review = Review.objects.get(id=review_id)
        except Review.DoesNotExist:
            raise Exception("Review not found")
        if review.user_id != user.id:
            raise Exception("You can only delete your own review.")
        review.delete()
        return DeleteReview(ok=True)


class ToggleFavoriteStation(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        station_id = graphene.ID(required=True)

    @login_required
    @active_required
    def mutate(self, info, station_id):
        user = info.context.user
        try:
            station = Station.objects.get(id=station_id)
        except Station.DoesNotExist:
            return ToggleFavoriteStation(success=False, message="Station not found")

        existing = Favorite.objects.filter(user=user, station=station).first()
        if existing:
            existing.delete()
            return ToggleFavoriteStation(success=True, message="Removed from favourites")

        # Only allow favouriting an available station.
        if not station.is_active:
            return ToggleFavoriteStation(success=False, message="Station is not available")

        # get_or_create + the unique constraint make this duplicate-safe.
        Favorite.objects.get_or_create(user=user, station=station)
        return ToggleFavoriteStation(success=True, message="Added to favourites")


class StationMutation(graphene.ObjectType):
    create_station = CreateStation.Field()
    update_station = UpdateStation.Field()
    delete_station = DeleteStation.Field()
    create_review = CreateReview.Field()
    update_review = UpdateReview.Field()
    delete_review = DeleteReview.Field()
    toggle_favorite_station = ToggleFavoriteStation.Field()


def _annotated_stations(user):
    """Active stations annotated with review stats and (auth) is_favorite."""
    qs = Station.objects.filter(is_active=True).annotate(
        num_of_rate=Count("reviews"),
        average_rate=Coalesce(Avg("reviews__rating"), 0.0),
    )
    if user.is_authenticated:
        favorite = Favorite.objects.filter(user=user, station=OuterRef("pk"))
        return qs.annotate(is_favorite=Exists(favorite))
    return qs.annotate(is_favorite=Value(False))


def _apply_station_filters(qs, charger_type, num_of_charger, min_rating, min_power, max_power):
    if charger_type:
        qs = qs.filter(charger_type__icontains=charger_type)
    if num_of_charger:
        qs = qs.filter(num_of_charger=num_of_charger)
    if min_rating is not None:
        qs = qs.filter(average_rate__gte=min_rating)
    if min_power is not None:
        qs = qs.filter(power_output_kw__gte=min_power)
    if max_power is not None:
        qs = qs.filter(power_output_kw__lte=max_power)
    return qs


def _window(qs, limit, offset):
    """Apply an explicit page window when requested, else the legacy hard cap."""
    if limit is not None:
        start = clamp_offset(offset)
        return qs[start:start + clamp_page_size(limit)]
    return hard_cap(qs)


def _station_filter_args():
    # Fresh argument instances per field (graphene must not share mounted args).
    return dict(
        charger_type=graphene.String(),
        num_of_charger=graphene.Int(),
        min_rating=graphene.Float(),
        min_power=graphene.Float(),
        max_power=graphene.Float(),
        limit=graphene.Int(),
        offset=graphene.Int(),
    )


class StationQuery(graphene.ObjectType):
    station_list = graphene.List(StationListType, limit=graphene.Int(), offset=graphene.Int())
    filter_stations = graphene.List(StationListType, **_station_filter_args())
    station_by_id = graphene.Field(StationType, station_id=graphene.ID(required=True))
    my_stations = graphene.List(StationListType, limit=graphene.Int(), offset=graphene.Int())

    # Proper paginated endpoints with a total count (Part 2).
    stations_page = graphene.Field(StationPage, **_station_filter_args())
    station_reviews = graphene.Field(
        ReviewPage, station_id=graphene.ID(required=True),
        limit=graphene.Int(), offset=graphene.Int(),
    )
    my_favorites = graphene.Field(StationPage, limit=graphene.Int(), offset=graphene.Int())

    def resolve_station_list(self, info, limit=None, offset=None):
        # Served from the cached public list; is_favorite overlaid per user.
        user = info.context.user
        rows = get_public_station_rows()
        fav_ids = set()
        if user.is_authenticated:
            fav_ids = set(
                Favorite.objects.filter(user=user).values_list("station_id", flat=True)
            )
        start = clamp_offset(offset)
        size = clamp_page_size(limit) if limit is not None else settings.GRAPHQL_LIST_HARD_CAP
        window = rows[start:start + size]
        return [
            StationListType(
                **{k: r[k] for k in _ROW_KEYS},
                is_favorite=(r["station_id"] in fav_ids),
            )
            for r in window
        ]

    def resolve_filter_stations(self, info, charger_type=None, num_of_charger=None,
                                min_rating=None, min_power=None, max_power=None,
                                limit=None, offset=None):
        qs = _apply_station_filters(
            _annotated_stations(info.context.user),
            charger_type, num_of_charger, min_rating, min_power, max_power,
        )
        return [_station_to_list_type(s) for s in _window(qs, limit, offset)]

    @login_required
    def resolve_my_stations(self, info, limit=None, offset=None):
        user = info.context.user
        qs = Station.objects.filter(owner=user, is_active=True).annotate(
            num_of_rate=Count("reviews"),
            average_rate=Coalesce(Avg("reviews__rating"), 0.0),
            is_favorite=Exists(Favorite.objects.filter(user=user, station=OuterRef("pk"))),
        )
        return [_station_to_list_type(s) for s in _window(qs, limit, offset)]

    def resolve_stations_page(self, info, charger_type=None, num_of_charger=None,
                              min_rating=None, min_power=None, max_power=None,
                              limit=None, offset=None):
        qs = _apply_station_filters(
            _annotated_stations(info.context.user),
            charger_type, num_of_charger, min_rating, min_power, max_power,
        ).order_by("id")
        items, total, has_next = paginate(qs, limit, offset)
        return StationPage(
            items=[_station_to_list_type(s) for s in items],
            total_count=total, has_next=has_next,
        )

    def resolve_station_reviews(self, info, station_id, limit=None, offset=None):
        qs = (
            Review.objects.filter(station_id=station_id)
            .select_related("user")
            .order_by("-created_at")
        )
        items, total, has_next = paginate(qs, limit, offset)
        return ReviewPage(items=items, total_count=total, has_next=has_next)

    @login_required
    def resolve_my_favorites(self, info, limit=None, offset=None):
        user = info.context.user
        qs = Station.objects.filter(favorited_by__user=user, is_active=True).annotate(
            num_of_rate=Count("reviews"),
            average_rate=Coalesce(Avg("reviews__rating"), 0.0),
        ).order_by("-id")
        items, total, has_next = paginate(qs, limit, offset)
        return StationPage(
            items=[_station_to_list_type(s, is_favorite=True) for s in items],
            total_count=total, has_next=has_next,
        )

    def resolve_station_by_id(self, info, station_id):
        try:
            return Station.objects.select_related("owner").get(id=station_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")
