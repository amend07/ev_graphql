import graphene
from graphene_django import DjangoObjectType
from django.contrib.auth import get_user_model
from graphql_jwt.decorators import login_required

from stations.models import Station
from stations.schema import StationType
from .permission import admin_required

User = get_user_model()

class UserType(DjangoObjectType):
    class Meta:
        model = User
        exclude = ("password",)

    favorites = graphene.List(lambda: StationType)

    def resolve_favorites(self, info):
        return Station.objects.filter(favorited_by__user=self)

class CreateUser(graphene.Mutation):
    user = graphene.Field(UserType)

    class Arguments:
        username = graphene.String(required=True)
        email = graphene.String(required=True)
        password = graphene.String(required=True)

    def mutate(self, info, username, email, password):
        # Prevent duplicate email/username
        if User.objects.filter(username=username).exists():
            raise Exception("Username already exists")
        if User.objects.filter(email=email).exists():
            raise Exception("Email already registered")

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            role="user"  # ✅ Secure: role is hardcoded
        )
        return CreateUser(user=user)


class RegisterStationOwner(graphene.Mutation):
    user = graphene.Field(UserType)

    class Arguments:
        username = graphene.String(required=True)
        email = graphene.String(required=True)
        password = graphene.String(required=True)

    def mutate(self, info, username, email, password):
        User = get_user_model()

        if User.objects.filter(username=username).exists():
            raise Exception("Username already exists")
        if User.objects.filter(email=email).exists():
            raise Exception("Email already registered")

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            role='station_owner',  # ✅ Fixed role
            is_active=False         # ✅ Must be approved by admin
        )

        return RegisterStationOwner(user=user)


class AdminQuery(graphene.ObjectType):
    users_by_role = graphene.List(UserType, role=graphene.String(required=True))
    unapproved_station_owners = graphene.List(UserType)

    @login_required
    @admin_required
    def resolve_users_by_role(self, info, role):
        return User.objects.filter(role=role)

    @login_required
    @admin_required
    def resolve_unapproved_station_owners(self, info):
        return User.objects.filter(role='station_owner', is_active=False)
    
class DeleteUser(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        user_id = graphene.ID(required=True)

    @login_required
    @admin_required
    def mutate(self, info, user_id):
        try:
            user = User.objects.get(pk=user_id)
            user.delete()
            return DeleteUser(ok=True)
        except User.DoesNotExist:
            return DeleteUser(ok=False)

class ToggleUserActive(graphene.Mutation):
    user = graphene.Field(UserType)

    class Arguments:
        user_id = graphene.ID(required=True)
        is_active = graphene.Boolean(required=True)

    @login_required
    @admin_required
    def mutate(self, info, user_id, is_active):
        try:
            user = User.objects.get(pk=user_id)
            user.is_active = is_active
            user.save()
            return ToggleUserActive(user=user)
        except User.DoesNotExist:
            raise Exception("User not found")
        
        
class AdminMutation(graphene.ObjectType):
    delete_user = DeleteUser.Field()
    toggle_user_active = ToggleUserActive.Field()
    
class AccountsQuery(graphene.ObjectType):
    me = graphene.Field(UserType)

    @login_required
    def resolve_me(self, info):
        return info.context.user

 
class ApproveStationOwner(graphene.Mutation):
    success = graphene.Boolean()
    user = graphene.Field(UserType)

    class Arguments:
        user_id = graphene.ID(required=True)

    @admin_required
    def mutate(self, info, user_id):
        User = get_user_model()

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise Exception("User not found")

        if user.role != 'station_owner':
            raise Exception("Only station owners require approval")

        user.is_active = True
        user.save()

        return ApproveStationOwner(success=True, user=user)


class AccountsMutation(graphene.ObjectType):
    create_user = CreateUser.Field()
    register_station_owner = RegisterStationOwner.Field()
    approve_station_owner = ApproveStationOwner.Field()
    
   