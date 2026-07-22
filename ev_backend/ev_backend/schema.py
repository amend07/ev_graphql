import graphene
from accounts.schema import (
    AccountsQuery,
    AccountsMutation,
    AdminQuery,
    AdminMutation,
)
from stations.schema import (
    StationAdminMutation,
    StationAdminQuery,
    StationMutation,
    StationQuery,
)
from bookings.schema import BookingAdminQuery, BookingMutation, BookingQuery
from notifications.schema import NotificationMutation, NotificationQuery
from vehicles.schema import VehicleMutation, VehicleQuery
from charging.schema import ChargingMutation, ChargingQuery


class Query(
    AccountsQuery,
    StationQuery,
    BookingQuery,
    AdminQuery,
    StationAdminQuery,
    BookingAdminQuery,
    NotificationQuery,
    VehicleQuery,
    ChargingQuery,
    graphene.ObjectType,
):
    pass


class Mutation(
    AccountsMutation,
    StationMutation,
    BookingMutation,
    AdminMutation,
    StationAdminMutation,
    NotificationMutation,
    VehicleMutation,
    ChargingMutation,
    graphene.ObjectType,
):
    token_auth = AccountsMutation.token_auth
    verify_token = AccountsMutation.verify_token
    refresh_token = AccountsMutation.refresh_token


schema = graphene.Schema(query=Query, mutation=Mutation)