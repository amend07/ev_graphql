import graphene
from graphene_file_upload.scalars import Upload
from graphene_django import DjangoObjectType
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Avg, Exists, OuterRef, Q, Value
from . import administration
from .models import Favorite, Review, Station, review_stats
from .validators import (
    InvalidInput,
    validate_station_input,
    validate_image,
    validate_rating,
    validate_comment,
    normalize_charger_type,
    normalize_charge_mode,
)
from .cache import get_public_station_rows
from accounts.permission import (
    active_required,
    admin_required,
    login_required,
    public,
    station_owner_required,
)
from ev_backend.pagination import apply_ordering, clamp_page_size, clamp_offset, paginate, window
from notifications.models import Notification
from notifications.service import notify

# Row keys shared between the cached station list and StationListType.
_ROW_KEYS = (
    "station_id", "name", "latitude", "longitude", "charger_type", "charge_mode",
    "num_of_charger", "num_of_rate", "average_rate", "power_output_kw",
    "price_per_kwh", "is_active", "image",
)

# Scalar Station fields accepted from input; used to build create/update payloads
# without ever passing an explicit None (which would clobber model defaults).
STATION_FIELDS = [
    "name", "contact_info", "description", "location", "latitude", "longitude",
    "availability", "amenities", "charger_type", "charge_mode", "station_count",
    "num_of_charger", "power_output_kw", "estimated_time_min", "price_per_kwh",
    "charger_brand",
]

# Availability filter values (Sprint W9). "available"/"occupied" map onto the
# station's is_active flag — the only occupancy signal the platform has; there is
# no per-charger live telemetry. "all" (or an absent filter) keeps the historical
# active-only default so anonymous discovery is unchanged unless a caller opts in.
AVAILABILITY_ALL = 'all'
AVAILABILITY_AVAILABLE = 'available'
AVAILABILITY_OCCUPIED = 'occupied'


class PublicUserType(graphene.ObjectType):
    """A user as strangers may see them: who they are, nothing more.

    Used wherever a user hangs off public data — a station's owner, a review's
    author. Before B1 these were full ``UserType`` objects, so
    ``stationById { owner { email isSuperuser } }`` answered for anonymous
    callers. Narrowing the type removes the reachability rather than guarding it,
    which is the only version that cannot be forgotten.

    Both clients only ever select `id`/`username` here, so this is the complete
    set they use.
    """

    id = graphene.ID(required=True)
    username = graphene.String(required=True)


def _public_user(user):
    return PublicUserType(id=user.id, username=user.username) if user else None


class StationType(DjangoObjectType):
    num_of_reviews = graphene.Int()
    average_rating = graphene.Float()
    is_favorite = graphene.Boolean()
    # required: Station.owner is a non-null FK, and the field was `UserType!`
    # before B1. Narrowing the type must not quietly widen its nullability.
    owner = graphene.Field(PublicUserType, required=True)

    class Meta:
        model = Station
        # charger_type/charge_mode gained `choices` in W9; without this,
        # graphene-django would convert them to generated enums and change
        # `chargerType`/`chargeMode` from String to an enum in the SDL — a
        # breaking change for both shipped clients. Keep them plain strings.
        convert_choices_to_enum = False
        # Explicit allow-list (B1 Phase 1). `__all__` silently published every
        # future field and every reverse relation — `bookings`, `reviews`,
        # `favorited_by` — which is how a station leaked its customers' identities
        # and movements to anonymous callers. Adding a field to the model no
        # longer adds it to the public API.
        fields = (
            "id", "name", "contact_info", "description", "image", "is_active",
            "location", "latitude", "longitude", "availability", "amenities",
            "charger_type", "charge_mode", "station_count", "num_of_charger",
            "power_output_kw", "estimated_time_min", "price_per_kwh",
            "charger_brand", "charger_code", "created_at",
        )

    def resolve_owner(self, info):
        # Narrowed on purpose: ownership is public (clients hide "Book" on your
        # own station), the owner's contact details are not.
        return _public_user(self.owner)

    # Hidden reviews never count (B2.1): a moderated review that still moves the
    # station's rating has not been moderated, only made harder to read. Both
    # paths below honour that — the annotation through `review_stats()`, the
    # fallback through the same filter.
    #
    # Prefer the annotation when the queryset supplied one (B3). `stationsPageAdmin`
    # annotates `review_stats()`, and these resolvers used to ignore it and
    # re-query per row: the aggregate was computed in SQL, discarded, then paid
    # for twice more per station — 22 queries for 10 rows, measured. The fallback
    # stays for single-row reads (`stationById`, mutation payloads) that have no
    # annotation to read.

    def resolve_num_of_reviews(self, info):
        annotated = getattr(self, 'num_of_rate', None)
        if annotated is not None:
            return annotated
        return self.reviews.filter(is_hidden=False).count()

    def resolve_average_rating(self, info):
        annotated = getattr(self, 'average_rate', None)
        if annotated is not None:
            return annotated
        return self.reviews.filter(is_hidden=False).aggregate(
            avg_rating=Avg("rating"),
        )["avg_rating"] or 0.0

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
    charge_mode = graphene.String(description="AC or DC.")
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
    charge_mode = graphene.String()
    num_of_charger = graphene.Int()
    num_of_rate = graphene.Int()
    average_rate = graphene.Float()
    power_output_kw = graphene.Float()
    price_per_kwh = graphene.String()
    # Occupancy signal for the availability filter: active == "available",
    # inactive == "occupied"/taken. The only status the platform tracks.
    is_active = graphene.Boolean()
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
        charge_mode=s.charge_mode,
        num_of_charger=s.num_of_charger,
        num_of_rate=getattr(s, "num_of_rate", 0),
        average_rate=round(getattr(s, "average_rate", 0.0), 1),
        power_output_kw=s.power_output_kw,
        price_per_kwh=str(s.price_per_kwh),
        is_active=s.is_active,
        is_favorite=getattr(s, "is_favorite", False) if is_favorite is None else is_favorite,
        image=s.image.url if s.image else "",
    )


def _input_to_data(input):
    """Provided (non-None) scalar fields only, so model defaults still apply.

    Connector and mode strings are folded onto their canonical codes here, so a
    client sending "Type 2" or "DC" is stored as "type2"/"dc" and matches the
    filter (which compares against canonical codes)."""
    data = {f: getattr(input, f) for f in STATION_FIELDS if getattr(input, f, None) is not None}
    if "charger_type" in data:
        data["charger_type"] = normalize_charger_type(data["charger_type"])
    if "charge_mode" in data:
        data["charge_mode"] = normalize_charge_mode(data["charge_mode"])
    return data


class CreateStation(graphene.Mutation):
    station = graphene.Field(StationType)

    class Arguments:
        input = CreateStationInput(required=True)
        image = Upload(required=False)

    @station_owner_required
    def mutate(self, info, input, image=None):
        user = info.context.user
        data = _input_to_data(input)

        try:
            validate_station_input(data, partial=False)
            validate_image(image)
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
            try:
                validate_image(image)
            except InvalidInput as e:
                raise Exception(str(e))
            station.image = image

        station.save()
        return UpdateStation(station=station)


class DeleteStation(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        stationId = graphene.ID(required=True)

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
    # required: Review.user is a non-null FK (was `UserType!`).
    user = graphene.Field(PublicUserType, required=True)

    class Meta:
        model = Review
        # Reviews are public (anyone may read a station's reviews), so the author
        # is narrowed to the display identity both clients actually select.
        # `station` is omitted: reviews are only ever reached through a station,
        # so the back-reference is dead weight and another traversal edge.
        fields = ("id", "rating", "comment", "created_at")

    def resolve_user(self, info):
        return _public_user(self.user)


class CreateReview(graphene.Mutation):
    review = graphene.Field(lambda: ReviewType)

    class Arguments:
        station_id = graphene.ID(required=True)
        rating = graphene.Int(required=True)
        comment = graphene.String()

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

        # Tell the owner their station was reviewed.
        notify(
            recipient=station.owner_id,
            notification_type=Notification.TYPE_REVIEW,
            title="New review",
            body=f"{user.username} left a {rating}-star review on {station.name}.",
            related_id=station.id,
        )
        return CreateReview(review=review)


class UpdateReview(graphene.Mutation):
    review = graphene.Field(lambda: ReviewType)

    class Arguments:
        review_id = graphene.ID(required=True)
        rating = graphene.Int()
        comment = graphene.String()

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


# ── Administration (Sprint B2.1) ─────────────────────────────────────────────
#
# Everything below is admin-gated. The public types above are reused where the
# audience genuinely sees the same thing (a station is public data; an admin
# needs no narrower view of it), and a separate type is introduced only where the
# audience differs — `AdminReviewType` publishes moderation state and the station
# a review belongs to, neither of which the public `ReviewType` should carry.


class AdminStationPage(graphene.ObjectType):
    items = graphene.List(StationType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


class AdminReviewType(graphene.ObjectType):
    """A review as a moderator sees it: the content, its author, and its state.

    Separate from `ReviewType` for two reasons, both rule 3:

    * it publishes `is_hidden`, which is moderation state the public read has no
      business carrying (there it would be constant `false` — every hidden review
      is already filtered out);
    * it publishes `station`, which `ReviewType` deliberately omits because a
      public review is only ever reached *through* a station. A moderation queue
      is the opposite: it spans stations, so a row that cannot say which station
      it belongs to is unusable.

    Hand-rolled rather than a second `DjangoObjectType` over `Review` so it does
    not overwrite `ReviewType` in graphene-django's per-model registry.
    """

    id = graphene.ID(required=True)
    rating = graphene.Int(required=True)
    comment = graphene.String(required=True)
    created_at = graphene.DateTime(required=True)
    is_hidden = graphene.Boolean(required=True)
    user = graphene.Field(PublicUserType, required=True)
    station = graphene.Field(StationType, required=True)


class AdminReviewPage(graphene.ObjectType):
    items = graphene.List(AdminReviewType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


def _admin_review(review):
    return AdminReviewType(
        id=review.id,
        rating=review.rating,
        comment=review.comment,
        created_at=review.created_at,
        is_hidden=review.is_hidden,
        user=_public_user(review.user),
        station=review.station,
    )


# Ordering allow-lists. Never interpolate a caller's string into `order_by`: on
# the user directory that would let someone sort by `password` and read the hash
# out one comparison at a time. The same discipline applies here even though
# these models hold no secret — the rule is the protection, not the model.
STATION_ADMIN_ORDER_FIELDS = {
    'newest': '-created_at',
    'oldest': 'created_at',
    'name': 'name',
    'rating': '-average_rate',
}

REVIEW_ORDER_FIELDS = {
    'newest': '-created_at',
    'oldest': 'created_at',
    'rating_high': '-rating',
    'rating_low': 'rating',
}


class StationAdminQuery(graphene.ObjectType):
    stations_page_admin = graphene.Field(
        AdminStationPage,
        search=graphene.String(),
        owner_id=graphene.ID(),
        is_active=graphene.Boolean(),
        charger_type=graphene.String(),
        min_rating=graphene.Float(),
        order_by=graphene.String(),
        limit=graphene.Int(),
        offset=graphene.Int(),
        description=(
            "Every station, INCLUDING inactive ones — unlike the public "
            "stationsPage, which only ever shows active stations. Admin only. "
            "search matches name, location or owner username."
        ),
    )
    reviews_page = graphene.Field(
        AdminReviewPage,
        station_id=graphene.ID(),
        owner_id=graphene.ID(),
        customer_id=graphene.ID(),
        rating=graphene.Int(),
        is_hidden=graphene.Boolean(),
        search=graphene.String(),
        order_by=graphene.String(),
        limit=graphene.Int(),
        offset=graphene.Int(),
        description=(
            "Review moderation queue. Sees hidden reviews too; filter "
            "isHidden: false for the queue of live reviews. Admin only."
        ),
    )

    @admin_required
    def resolve_stations_page_admin(self, info, search=None, owner_id=None,
                                    is_active=None, charger_type=None,
                                    min_rating=None, order_by=None,
                                    limit=None, offset=None):
        # No `is_active=True` filter: seeing what has been taken down is the
        # point of the admin view, and B1's "no reactivation path" deviation
        # existed partly because nothing could list an inactive station at all.
        qs = Station.objects.select_related("owner").annotate(**review_stats())

        if owner_id:
            qs = qs.filter(owner_id=owner_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if charger_type:
            qs = qs.filter(charger_type__icontains=charger_type)
        if min_rating is not None:
            qs = qs.filter(average_rate__gte=min_rating)
        if search:
            term = search.strip()
            if term:
                qs = qs.filter(
                    Q(name__icontains=term)
                    | Q(location__icontains=term)
                    | Q(owner__username__icontains=term)
                )

        qs = apply_ordering(qs, order_by, STATION_ADMIN_ORDER_FIELDS, '-created_at')

        items, total, has_next = paginate(qs, limit, offset)
        return AdminStationPage(items=items, total_count=total, has_next=has_next)

    @admin_required
    def resolve_reviews_page(self, info, station_id=None, owner_id=None,
                             customer_id=None, rating=None, is_hidden=None,
                             search=None, order_by=None, limit=None, offset=None):
        qs = Review.objects.select_related("user", "station", "station__owner")

        if station_id:
            qs = qs.filter(station_id=station_id)
        if owner_id:
            qs = qs.filter(station__owner_id=owner_id)
        if customer_id:
            qs = qs.filter(user_id=customer_id)
        if rating is not None:
            qs = qs.filter(rating=rating)
        if is_hidden is not None:
            qs = qs.filter(is_hidden=is_hidden)
        if search:
            term = search.strip()
            if term:
                # The comment is what a moderator is usually hunting for — a
                # reported phrase — plus the author and station to reach it from
                # a complaint naming either.
                qs = qs.filter(
                    Q(comment__icontains=term)
                    | Q(user__username__icontains=term)
                    | Q(station__name__icontains=term)
                )

        qs = apply_ordering(qs, order_by, REVIEW_ORDER_FIELDS, '-created_at')

        items, total, has_next = paginate(qs, limit, offset)
        return AdminReviewPage(
            items=[_admin_review(r) for r in items],
            total_count=total, has_next=has_next,
        )


class ActivateStation(graphene.Mutation):
    """Bring a station back into service.

    Closes B1's known deviation: a station taken down (by its owner's soft-delete
    or by an admin) had no route back short of database access.
    """

    station = graphene.Field(StationType)

    class Arguments:
        station_id = graphene.ID(required=True)
        reason = graphene.String()

    @admin_required
    def mutate(self, info, station_id, reason=None):
        station = administration.get_station(station_id)
        station = administration.set_station_active(
            actor=info.context.user, station=station, is_active=True,
            reason=reason, request=info.context,
        )
        return ActivateStation(station=station)


class DeactivateStation(graphene.Mutation):
    """Withdraw a station from discovery and block new bookings.

    Existing bookings are left alone — see `set_station_active`.
    """

    station = graphene.Field(StationType)

    class Arguments:
        station_id = graphene.ID(required=True)
        reason = graphene.String()

    @admin_required
    def mutate(self, info, station_id, reason=None):
        station = administration.get_station(station_id)
        station = administration.set_station_active(
            actor=info.context.user, station=station, is_active=False,
            reason=reason, request=info.context,
        )
        return DeactivateStation(station=station)


class HideReview(graphene.Mutation):
    """Remove a review from every public read and from its station's rating."""

    review = graphene.Field(AdminReviewType)

    class Arguments:
        review_id = graphene.ID(required=True)
        reason = graphene.String()

    @admin_required
    def mutate(self, info, review_id, reason=None):
        review = administration.get_review(review_id)
        review = administration.set_review_hidden(
            actor=info.context.user, review=review, is_hidden=True,
            reason=reason, request=info.context,
        )
        return HideReview(review=_admin_review(review))


class RestoreReview(graphene.Mutation):
    """Un-hide a review. The reason hiding is preferred over deleting."""

    review = graphene.Field(AdminReviewType)

    class Arguments:
        review_id = graphene.ID(required=True)
        reason = graphene.String()

    @admin_required
    def mutate(self, info, review_id, reason=None):
        review = administration.get_review(review_id)
        review = administration.set_review_hidden(
            actor=info.context.user, review=review, is_hidden=False,
            reason=reason, request=info.context,
        )
        return RestoreReview(review=_admin_review(review))


class AdminDeleteReview(graphene.Mutation):
    """Destroy someone else's review. Irreversible; a reason is required.

    Named `adminDeleteReview`, NOT `deleteReview`: `deleteReview` already exists
    and is the author-only mutation both clients call. Reusing the name would
    either break those clients or, worse, silently widen an author-only action
    into an admin one on a field they already have documents for.
    """

    ok = graphene.Boolean()

    class Arguments:
        review_id = graphene.ID(required=True)
        reason = graphene.String(required=True)

    @admin_required
    def mutate(self, info, review_id, reason):
        review = administration.get_review(review_id)
        administration.delete_review(
            actor=info.context.user, review=review, reason=reason,
            request=info.context,
        )
        return AdminDeleteReview(ok=True)


class StationAdminMutation(graphene.ObjectType):
    activate_station = ActivateStation.Field()
    deactivate_station = DeactivateStation.Field()
    hide_review = HideReview.Field()
    restore_review = RestoreReview.Field()
    admin_delete_review = AdminDeleteReview.Field()


def _annotated_stations(user, *, active_only=True):
    """Stations annotated with review stats and (auth) is_favorite.

    ``active_only`` defaults True so anonymous discovery keeps showing only live
    stations. The availability filter flips it off to reach taken-down ("occupied")
    stations — see ``_availability_scope``."""
    base = Station.objects.filter(is_active=True) if active_only else Station.objects.all()
    qs = base.annotate(**review_stats())
    if user.is_authenticated:
        favorite = Favorite.objects.filter(user=user, station=OuterRef("pk"))
        return qs.annotate(is_favorite=Exists(favorite))
    return qs.annotate(is_favorite=Value(False))


def _availability_scope(availability):
    """Resolve the availability filter to ``(active_only_base, is_active_eq)``.

    * ``None``       — historical default: only active stations, no explicit filter.
    * ``all``        — both active and inactive.
    * ``available``  — active only.
    * ``occupied``   — inactive only (taken down / out of service).
    """
    if availability == AVAILABILITY_ALL:
        return False, None
    if availability == AVAILABILITY_AVAILABLE:
        return True, True
    if availability == AVAILABILITY_OCCUPIED:
        return False, False
    return True, None  # None or anything unrecognised: unchanged behaviour


def _apply_station_filters(qs, *, charger_type=None, charge_mode=None,
                           num_of_charger=None, min_rating=None, max_rating=None,
                           min_power=None, max_power=None, min_price=None,
                           max_price=None, is_active=None):
    if charger_type:
        # Exact (case-insensitive) match on the canonical code, so "ccs" no longer
        # also matches "ccs2". Input is normalised so "Type 2"/"CCS" still resolve.
        qs = qs.filter(charger_type__iexact=normalize_charger_type(charger_type))
    if charge_mode:
        qs = qs.filter(charge_mode__iexact=normalize_charge_mode(charge_mode))
    if num_of_charger:
        qs = qs.filter(num_of_charger=num_of_charger)
    if min_rating is not None:
        qs = qs.filter(average_rate__gte=min_rating)
    if max_rating is not None:
        qs = qs.filter(average_rate__lte=max_rating)
    if min_power is not None:
        qs = qs.filter(power_output_kw__gte=min_power)
    if max_power is not None:
        qs = qs.filter(power_output_kw__lte=max_power)
    if min_price is not None:
        qs = qs.filter(price_per_kwh__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price_per_kwh__lte=max_price)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    return qs


def _station_filter_args():
    # Fresh argument instances per field (graphene must not share mounted args).
    return dict(
        charger_type=graphene.String(),
        charge_mode=graphene.String(description="AC or DC."),
        num_of_charger=graphene.Int(),
        min_rating=graphene.Float(),
        max_rating=graphene.Float(),
        min_power=graphene.Float(),
        max_power=graphene.Float(),
        min_price=graphene.Float(),
        max_price=graphene.Float(),
        availability=graphene.String(
            description="all | available | occupied. 'occupied' surfaces "
                        "out-of-service stations; absent keeps active-only.",
        ),
        limit=graphene.Int(),
        offset=graphene.Int(),
    )


class StationQuery(graphene.ObjectType):
    station_list = graphene.List(StationListType, limit=graphene.Int(), offset=graphene.Int())
    filter_stations = graphene.List(StationListType, **_station_filter_args())
    station_by_id = graphene.Field(StationType, station_id=graphene.ID(required=True))
    my_stations = graphene.List(StationListType, limit=graphene.Int(), offset=graphene.Int())
    # Paginated twin of my_stations (totalCount + hasNext), so the owner's "My
    # Stations" list can page. my_stations stays for the shipped client.
    my_stations_page = graphene.Field(
        StationPage, limit=graphene.Int(), offset=graphene.Int(),
    )

    # Proper paginated endpoints with a total count (Part 2).
    stations_page = graphene.Field(StationPage, **_station_filter_args())
    station_reviews = graphene.Field(
        ReviewPage, station_id=graphene.ID(required=True),
        limit=graphene.Int(), offset=graphene.Int(),
    )
    my_favorites = graphene.Field(StationPage, limit=graphene.Int(), offset=graphene.Int())

    @public
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

    @public
    def resolve_filter_stations(self, info, charger_type=None, charge_mode=None,
                                num_of_charger=None, min_rating=None, max_rating=None,
                                min_power=None, max_power=None, min_price=None,
                                max_price=None, availability=None,
                                limit=None, offset=None):
        active_only, is_active = _availability_scope(availability)
        qs = _apply_station_filters(
            _annotated_stations(info.context.user, active_only=active_only),
            charger_type=charger_type, charge_mode=charge_mode,
            num_of_charger=num_of_charger, min_rating=min_rating,
            max_rating=max_rating, min_power=min_power, max_power=max_power,
            min_price=min_price, max_price=max_price, is_active=is_active,
        )
        return [_station_to_list_type(s) for s in window(qs, limit, offset)]

    @login_required
    def resolve_my_stations(self, info, limit=None, offset=None):
        user = info.context.user
        qs = Station.objects.filter(owner=user, is_active=True).annotate(
            is_favorite=Exists(Favorite.objects.filter(user=user, station=OuterRef("pk"))),
            **review_stats(),
        )
        return [_station_to_list_type(s) for s in window(qs, limit, offset)]

    @login_required
    def resolve_my_stations_page(self, info, limit=None, offset=None):
        # Owner-scoped and active-only, exactly like my_stations. Deterministic
        # order (newest first, id as tie-break) so paging never skips or repeats.
        user = info.context.user
        qs = Station.objects.filter(owner=user, is_active=True).annotate(
            is_favorite=Exists(Favorite.objects.filter(user=user, station=OuterRef("pk"))),
            **review_stats(),
        ).order_by("-created_at", "-id")
        items, total, has_next = paginate(qs, limit, offset)
        return StationPage(
            items=[_station_to_list_type(s) for s in items],
            total_count=total, has_next=has_next,
        )

    @public
    def resolve_stations_page(self, info, charger_type=None, charge_mode=None,
                              num_of_charger=None, min_rating=None, max_rating=None,
                              min_power=None, max_power=None, min_price=None,
                              max_price=None, availability=None,
                              limit=None, offset=None):
        active_only, is_active = _availability_scope(availability)
        qs = _apply_station_filters(
            _annotated_stations(info.context.user, active_only=active_only),
            charger_type=charger_type, charge_mode=charge_mode,
            num_of_charger=num_of_charger, min_rating=min_rating,
            max_rating=max_rating, min_power=min_power, max_power=max_power,
            min_price=min_price, max_price=max_price, is_active=is_active,
        ).order_by("id")
        items, total, has_next = paginate(qs, limit, offset)
        return StationPage(
            items=[_station_to_list_type(s) for s in items],
            total_count=total, has_next=has_next,
        )

    @public
    def resolve_station_reviews(self, info, station_id, limit=None, offset=None):
        # `Review.visible()` — a hidden review is gone from the public read, not
        # merely flagged in it (B2.1).
        qs = (
            Review.visible()
            .filter(station_id=station_id)
            .select_related("user")
            .order_by("-created_at", "-id")
        )
        items, total, has_next = paginate(qs, limit, offset)
        return ReviewPage(items=items, total_count=total, has_next=has_next)

    @login_required
    def resolve_my_favorites(self, info, limit=None, offset=None):
        user = info.context.user
        qs = Station.objects.filter(
            favorited_by__user=user, is_active=True,
        ).annotate(**review_stats()).order_by("-id")
        items, total, has_next = paginate(qs, limit, offset)
        return StationPage(
            items=[_station_to_list_type(s, is_favorite=True) for s in items],
            total_count=total, has_next=has_next,
        )

    @public
    def resolve_station_by_id(self, info, station_id):
        try:
            return Station.objects.select_related("owner").get(id=station_id)
        except Station.DoesNotExist:
            raise Exception("Station not found")
