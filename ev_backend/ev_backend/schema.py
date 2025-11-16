import graphene
from accounts.schema import (
    AccountsQuery,
    AccountsMutation,
    AdminQuery,
    AdminMutation,
)
from stations.schema import StationMutation, StationQuery
from bookings.schema import BookingMutation, BookingQuery


class Query(
    AccountsQuery,
    StationQuery,
    BookingQuery,
    AdminQuery,
    graphene.ObjectType,
):
    pass


class Mutation(
    AccountsMutation,
    StationMutation,
    BookingMutation,
    AdminMutation,
    graphene.ObjectType,
):
    token_auth = AccountsMutation.token_auth
    verify_token = AccountsMutation.verify_token
    refresh_token = AccountsMutation.refresh_token


schema = graphene.Schema(query=Query, mutation=Mutation)