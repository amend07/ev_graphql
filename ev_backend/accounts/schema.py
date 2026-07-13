import graphene
import graphql_jwt
from graphene_django import DjangoObjectType
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.utils import timezone
from graphql_jwt.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from stations.models import Station
from stations.schema import StationType
from .permission import admin_required
from .models import User, PasswordResetOTP

User = get_user_model()


def _invalidate_other_sessions(user, info=None):
    """Best-effort invalidation of a user's existing auth after a PIN change.

    Changing ``user.password`` already rotates the value Django's session-auth
    hash is derived from, so other Django sessions stop authenticating. When an
    authenticated request is available we call ``update_session_auth_hash`` so
    the *current* caller stays signed in while siblings are dropped. If the
    long-running JWT refresh-token app is installed, its tokens are revoked so
    expired access tokens cannot be renewed.

    Stateless JWT access tokens already issued remain valid until they expire;
    revoking those mid-flight would require a token blacklist, i.e. an
    authentication-architecture change that is intentionally out of scope here.
    """
    request = getattr(info, "context", None) if info is not None else None
    if request is not None and getattr(request, "user", None) == user:
        try:
            update_session_auth_hash(request, user)
        except Exception:
            pass

    try:  # revoke stored refresh tokens only if that app is installed
        from graphql_jwt.refresh_token.models import RefreshToken

        RefreshToken.objects.filter(user=user, revoked__isnull=True).update(
            revoked=timezone.now()
        )
    except Exception:
        pass


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
    """Custom tokenAuth that returns token, refreshToken, and user.

    The credential is exposed as ``pin`` (not ``password``). graphql_jwt's
    ``JSONWebTokenMutation.Field`` hard-injects a required ``password`` argument
    and its ``@token_auth`` decorator reads ``password`` from kwargs, so we
    override ``Field`` to advertise ``pin`` and translate it back to
    ``password`` before delegating to graphql_jwt.
    """
    user = graphene.Field(UserType)
    refresh_token = graphene.String()

    @classmethod
    def Field(cls, *args, **kwargs):
        cls._meta.arguments.update({
            get_user_model().USERNAME_FIELD: graphene.String(required=True),
            "pin": graphene.String(required=True),
        })
        # Skip JSONWebTokenMutation.Field (it would re-add a required `password`).
        return super(graphql_jwt.JSONWebTokenMutation, cls).Field(*args, **kwargs)

    @classmethod
    def resolve(cls, root, info, **kwargs):
        return cls(user=info.context.user)

    @classmethod
    def mutate(cls, root, info, **input):
        username = input.get("username")
        pin = input.get("pin")

        # Login intentionally does NOT enforce the 6-digit format: pre-existing
        # accounts created under the old policy must still be able to sign in
        # (backward compatibility). The new format is enforced wherever a
        # credential is *set* — registration, PIN reset, and PIN change.
        if not pin:
            raise Exception("PIN is required.")

        if username and "@" in username:
            try:
                user_obj = User.objects.get(email__iexact=username)
                username = user_obj.username
            except User.DoesNotExist:
                pass

        # graphql_jwt authenticates on the `password` kwarg; hand it the PIN.
        result = super().mutate(root, info, username=username, password=pin)

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
        pin = graphene.String(required=True)
        is_station_owner = graphene.Boolean(required=False, default_value=False)

    def mutate(self, info, username, email, pin, is_station_owner=False):
        # Enforce the 6-digit PIN policy before creating the account.
        try:
            validate_password(pin)
        except ValidationError as e:
            raise Exception("; ".join(e.messages))

        if User.objects.filter(username=username).exists():
            raise Exception("Username already exists")
        if User.objects.filter(email=email).exists():
            raise Exception("Email already registered")

        role = "station_owner" if is_station_owner else "user"
        # create_user hashes the PIN via Django's configured password hasher;
        # it is never persisted in plaintext.
        user = User.objects.create_user(
            username=username,
            email=email,
            password=pin,
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


class SendPinResetOtp(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)

    def mutate(self, info, email):
        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return SendPinResetOtp(
                success=True,
                message="If the email is registered, an OTP has been sent."
            )

        try:
            PasswordResetOTP.generate_for_user(user)
            return SendPinResetOtp(success=True, message="OTP sent to your email.")
        except Exception:
            return SendPinResetOtp(success=False, message="Failed to send OTP.")


class ResetPinWithOtp(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)
        otp = graphene.String(required=True)
        new_pin = graphene.String(required=True)

    def mutate(self, info, email, otp, new_pin):
        if len(otp) != 6 or not otp.isdigit():
            return ResetPinWithOtp(success=False, message="OTP must be a 6-digit number")

        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return ResetPinWithOtp(success=False, message="Invalid request.")

        try:
            reset_obj = PasswordResetOTP.objects.get(user=user)
        except PasswordResetOTP.DoesNotExist:
            return ResetPinWithOtp(success=False, message="No OTP request found.")

        if not reset_obj.is_valid(otp):
            reset_obj.delete()
            return ResetPinWithOtp(success=False, message="Invalid or expired OTP.")

        try:
            validate_password(new_pin, user)
        except ValidationError as e:
            return ResetPinWithOtp(success=False, message="; ".join(e.messages))

        # set_password hashes the new PIN; it is never stored in plaintext.
        user.set_password(new_pin)
        user.save()
        reset_obj.delete()
        # A reset is account recovery — drop any renewable sessions/tokens.
        _invalidate_other_sessions(user, info)
        return ResetPinWithOtp(
            success=True,
            message="PIN reset successfully."
        )


class ChangePin(graphene.Mutation):
    """Authenticated user changes their own PIN."""
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        current_pin = graphene.String(required=True)
        new_pin = graphene.String(required=True)

    @login_required
    def mutate(self, info, current_pin, new_pin):
        user = info.context.user

        if not user.check_password(current_pin):
            return ChangePin(
                success=False,
                message="Current PIN is incorrect."
            )

        try:
            validate_password(new_pin, user)
        except ValidationError as e:
            return ChangePin(
                success=False,
                message="; ".join(e.messages)
            )

        # set_password hashes the new PIN; it is never stored in plaintext.
        user.set_password(new_pin)
        user.save()
        # Keep this caller signed in, invalidate the user's other sessions.
        _invalidate_other_sessions(user, info)

        return ChangePin(
            success=True,
            message="PIN changed successfully."
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
    send_pin_reset_otp = SendPinResetOtp.Field()
    reset_pin_with_otp = ResetPinWithOtp.Field()
    change_pin = ChangePin.Field()

    token_auth = CustomObtainJSONWebToken.Field()
    verify_token = graphql_jwt.Verify.Field()
    refresh_token = graphql_jwt.Refresh.Field()