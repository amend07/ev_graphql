import graphene
import graphql_jwt
from accounts.schema import AccountsQuery, AccountsMutation, AdminQuery, AdminMutation
from stations.schema import StationMutation, StationQuery
from bookings.schema import BookingMutation, BookingQuery
class Query(AccountsQuery, StationQuery, BookingQuery, AdminQuery, graphene.ObjectType):
    pass

class Mutation(AccountsMutation, StationMutation, BookingMutation, AdminMutation, graphene.ObjectType):
    token_auth = graphql_jwt.ObtainJSONWebToken.Field()
    verify_token = graphql_jwt.Verify.Field()
    refresh_token = graphql_jwt.Refresh.Field()

schema = graphene.Schema(query=Query, mutation=Mutation)