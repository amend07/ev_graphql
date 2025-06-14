import graphene
import graphql_jwt
from accounts.schema import AccountsQuery, AccountsMutation
from stations.schema import StationMutation, StationQuery
class Query(AccountsQuery, StationQuery, graphene.ObjectType):
    pass

class Mutation(AccountsMutation, graphene.ObjectType):
    token_auth = graphql_jwt.ObtainJSONWebToken.Field()
    verify_token = graphql_jwt.Verify.Field()
    refresh_token = graphql_jwt.Refresh.Field()

schema = graphene.Schema(query=Query, mutation=Mutation)

class Mutation(AccountsMutation, StationMutation, graphene.ObjectType):
    pass
