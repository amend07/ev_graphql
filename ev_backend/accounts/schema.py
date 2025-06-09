import graphene
from graphene_django import DjangoObjectType
from .models import User
from django.contrib.auth import get_user_model
from graphql_jwt.decorators import login_required

class UserType(DjangoObjectType):
    class Meta:
        model = get_user_model()
        exclude = ('password', )

class CreateUser(graphene.Mutation):
    user = graphene.Field(UserType)

    class Arguments:
        username = graphene.String(required=True)
        email = graphene.String(required=True)
        password = graphene.String(required=True)
        role = graphene.String(required=True)

    def mutate(self, info, username, email, password, role):
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password=password,
            role=role
        )
        return CreateUser(user=user)

class AccountsQuery(graphene.ObjectType):
    me = graphene.Field(UserType)

    @login_required
    def resolve_me(self, info):
        return info.context.user

class AccountsMutation(graphene.ObjectType):
    create_user = CreateUser.Field()
