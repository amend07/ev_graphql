import graphene
import graphql_jwt
from graphene_django import DjangoObjectType
from django.contrib.auth import get_user_model
from django.utils import timezone
from graphql_jwt.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from stations.models import Station
from stations.schema import StationType
from .permission import admin_required
from .models import User, PasswordResetOTP

User = get_user_model()


class UserType(DjangoObjectType):
    is_station_owner = graphene.Boolean()

    class Meta:
        model = User
        exclude = ("password",)

    favorites = graphene.List(lambda: StationType)

    def resolve_favorites(self, info):
        return Station.objects.filter(favorited_by__user=self)

    def resolve_is_station_owner(self, info):
        return self.role == "station_owner"

class OTPType(DjangoObjectType):
    is_valid = graphene.Boolean()

    class Meta:
        model = PasswordResetOTP
        fields = ("otp", "created_at", "expires_at")

    def resolve_is_valid(self, info):
        return timezone.now() <= self.expires_at

class CustomObtainJSONWebToken(graphql_jwt.JSONWebTokenMutation):
    """Custom tokenAuth that returns token, refreshToken, and user."""
    user = graphene.Field(UserType)
    refresh_token = graphene.String()

    class Arguments:
        username = graphene.String(required=True)
        password = graphene.String(required=True)

    @classmethod
    def resolve(cls, root, info, **kwargs):
        return cls(user=info.context.user)

    @classmethod
    def mutate(cls, root, info, **input):
        username = input.get("username")
        password = input.get("password")

        if username and "@" in username:
            try:
                user_obj = User.objects.get(email__iexact=username)
                username = user_obj.username
            except User.DoesNotExist:
                pass

        result = super().mutate(root, info, username=username, password=password)

        result.refresh_token = result.refresh_token
        result.user = info.context.user
        return result


class CreateUser(graphene.Mutation):
    user = graphene.Field(UserType)
    success = graphene.Boolean()
    user_id = graphene.ID()

    class Arguments:
        username = graphene.String(required=True)
        email = graphene.String(required=True)
        password = graphene.String(required=True)
        is_station_owner = graphene.Boolean(required=False, default_value=False)

    def mutate(self, info, username, email, password, is_station_owner=False):
        if User.objects.filter(username=username).exists():
            raise Exception("Username already exists")
        if User.objects.filter(email=email).exists():
            raise Exception("Email already registered")

        role = "station_owner" if is_station_owner else "user"
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            role=role,
        )
        return CreateUser(success=True, user_id=user.id, user=user)


class ApproveStationOwner(graphene.Mutation):
    success = graphene.Boolean()
    user = graphene.Field(UserType)

    class Arguments:
        user_id = graphene.ID(required=True)

    @admin_required
    def mutate(self, info, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise Exception("User not found")

        if user.role != "station_owner":
            raise Exception("Only station owners require approval")

        user.is_active = True
        user.save()
        return ApproveStationOwner(success=True, user=user)


class SendPasswordResetOtp(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)

    def mutate(self, info, email):
        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return SendPasswordResetOtp(
                success=True,
                message="If the email is registered, an OTP has been sent."
            )

        try:
            PasswordResetOTP.generate_for_user(user)
            return SendPasswordResetOtp(success=True, message="OTP sent to your email.")
        except Exception:
            return SendPasswordResetOtp(success=False, message="Failed to send OTP.")


class ResetPasswordWithOtp(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)
        otp = graphene.String(required=True)
        new_password = graphene.String(required=True)

    def mutate(self, info, email, otp, new_password):
        if len(otp) != 6 or not otp.isdigit():
            return ResetPasswordWithOtp(success=False, message="OTP must be a 6-digit number")

        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return ResetPasswordWithOtp(success=False, message="Invalid request.")

        try:
            reset_obj = PasswordResetOTP.objects.get(user=user)
        except PasswordResetOTP.DoesNotExist:
            return ResetPasswordWithOtp(success=False, message="No OTP request found.")

        if not reset_obj.is_valid(otp):
            reset_obj.delete()
            return ResetPasswordWithOtp(success=False, message="Invalid or expired OTP.")

        try:
            validate_password(new_password, user)
        except ValidationError as e:
            return ResetPasswordWithOtp(success=False, message="; ".join(e.messages))

        user.set_password(new_password)
        user.save()
        reset_obj.delete()
        return ResetPasswordWithOtp(
            success=True,
            message="Password reset successfully."
        )


class ChangePassword(graphene.Mutation):
    """Authenticated user changes their own password."""
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        current_password = graphene.String(required=True)
        new_password = graphene.String(required=True)

    @login_required
    def mutate(self, info, current_password, new_password):
        user = info.context.user

        if not user.check_password(current_password):
            return ChangePassword(
                success=False,
                message="Current password is incorrect."
            )

        try:
            validate_password(new_password, user)
        except ValidationError as e:
            return ChangePassword(
                success=False,
                message="; ".join(e.messages)
            )

        user.set_password(new_password)
        user.save()

        return ChangePassword(
            success=True,
            message="Password changed successfully."
        )

class AdminQuery(graphene.ObjectType):
    users_by_role = graphene.List(UserType, role=graphene.String(required=True))
    all_otps = graphene.List(OTPType)

    @login_required
    @admin_required
    def resolve_users_by_role(self, info, role):
        return User.objects.filter(role=role)

    @login_required
    @admin_required
    def resolve_all_otps(self, info):
        return PasswordResetOTP.objects.all()


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


class AccountsMutation(graphene.ObjectType):
    create_user = CreateUser.Field()
    approve_station_owner = ApproveStationOwner.Field()
    send_password_reset_otp = SendPasswordResetOtp.Field()
    reset_password_with_otp = ResetPasswordWithOtp.Field()
    change_password = ChangePassword.Field()

    token_auth = CustomObtainJSONWebToken.Field()
    verify_token = graphql_jwt.Verify.Field()
    refresh_token = graphql_jwt.Refresh.Field()