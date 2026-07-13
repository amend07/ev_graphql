import logging

import graphene
import graphql_jwt
from graphene_django import DjangoObjectType
from django.contrib.auth import get_user_model
from graphql_jwt.decorators import login_required

from stations.models import Station
from stations.schema import StationType
from .permission import admin_required
from .models import User, PasswordResetOTP
from . import services, ratelimit
from .auth_logging import log_event

User = get_user_model()


class UserType(DjangoObjectType):
    is_station_owner = graphene.Boolean()

    class Meta:
        model = User
        exclude = ("password",)  # credential hash is never exposed

    favorites = graphene.List(lambda: StationType)

    def resolve_favorites(self, info):
        return Station.objects.filter(favorited_by__user=self)

    def resolve_is_station_owner(self, info):
        return self.role == "station_owner"


class OTPType(DjangoObjectType):
    is_valid = graphene.Boolean()

    class Meta:
        model = PasswordResetOTP
        # Only non-sensitive metadata — never the code or its hash.
        fields = ("created_at", "expires_at", "attempts")

    def resolve_is_valid(self, info):
        return not self.is_expired()


class CustomObtainJSONWebToken(graphql_jwt.JSONWebTokenMutation):
    """tokenAuth returning token, refreshToken, and user.

    The credential is accepted as ``pin`` (canonical) or ``password`` (legacy,
    for the existing Flutter client). graphql_jwt's ``JSONWebTokenMutation.Field``
    hard-injects a required ``password`` arg and its ``@token_auth`` decorator
    reads ``password``; we override ``Field`` to advertise both as optional and
    translate whichever is supplied into ``password`` for authentication.

    Rate-limited per IP and per account; failures return a generic message and
    are logged (without any credential) to prevent account enumeration.
    """
    user = graphene.Field(UserType)
    refresh_token = graphene.String()

    @classmethod
    def Field(cls, *args, **kwargs):
        cls._meta.arguments.update({
            get_user_model().USERNAME_FIELD: graphene.String(required=True),
            "pin": graphene.String(required=False),
            "password": graphene.String(required=False),  # legacy alias
        })
        # Skip JSONWebTokenMutation.Field (it would re-add a required `password`).
        return super(graphql_jwt.JSONWebTokenMutation, cls).Field(*args, **kwargs)

    @classmethod
    def resolve(cls, root, info, **kwargs):
        return cls(user=info.context.user)

    @classmethod
    def mutate(cls, root, info, **input):
        request = info.context
        ip = ratelimit.get_client_ip(request)
        username = input.get("username")
        credential = input.get("pin") or input.get("password")
        if not credential:
            raise Exception("PIN is required.")

        # Brute-force protection: per IP and per account.
        try:
            ratelimit.enforce("LOGIN", ip, "ip")
            if username:
                ratelimit.enforce("LOGIN", username.lower(), "account")
        except ratelimit.RateLimitExceeded:
            log_event("account_locked", request=request, level=logging.WARNING,
                      reason="login_attempts")
            raise Exception(services.GENERIC_RATE_LIMITED)

        # Allow signing in with an email as well as a username.
        if username and "@" in username:
            found = User.objects.filter(email__iexact=username).first()
            if found:
                username = found.username

        try:
            result = super().mutate(root, info, username=username, password=credential)
        except Exception:
            log_event("login_failure", request=request)
            raise Exception("Please enter valid credentials")  # generic

        user = getattr(info.context, "user", None)
        ratelimit.reset("LOGIN", ip, "ip")
        if username:
            ratelimit.reset("LOGIN", username.lower(), "account")
        log_event("login_success", request=request, user=user)

        result.refresh_token = result.refresh_token
        result.user = user
        return result


class CreateUser(graphene.Mutation):
    user = graphene.Field(UserType)
    success = graphene.Boolean()
    user_id = graphene.ID()

    class Arguments:
        username = graphene.String(required=True)
        email = graphene.String(required=True)
        pin = graphene.String(required=False)
        password = graphene.String(required=False)  # legacy alias
        is_station_owner = graphene.Boolean(required=False, default_value=False)

    def mutate(self, info, username, email, pin=None, password=None, is_station_owner=False):
        credential = pin or password
        if not credential:
            raise Exception("PIN is required.")
        try:
            user = services.create_account(
                username, email, credential, is_station_owner, request=info.context
            )
        except services.CredentialError as e:
            raise Exception(str(e))
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


# ── Canonical PIN mutations ──────────────────────────────────────────────────

class SendPinResetOtp(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)

    def mutate(self, info, email):
        try:
            message = services.send_reset_otp(email, request=info.context)
        except ratelimit.RateLimitExceeded:
            return SendPinResetOtp(success=False, message=services.GENERIC_RATE_LIMITED)
        return SendPinResetOtp(success=True, message=message)


class ResetPinWithOtp(graphene.Mutation):
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)
        otp = graphene.String(required=True)
        new_pin = graphene.String(required=True)

    def mutate(self, info, email, otp, new_pin):
        try:
            success, message = services.reset_pin(email, otp, new_pin, request=info.context)
        except ratelimit.RateLimitExceeded:
            return ResetPinWithOtp(success=False, message=services.GENERIC_RATE_LIMITED)
        return ResetPinWithOtp(success=success, message=message)


class ChangePin(graphene.Mutation):
    """Authenticated user changes their own PIN."""
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        current_pin = graphene.String(required=True)
        new_pin = graphene.String(required=True)

    @login_required
    def mutate(self, info, current_pin, new_pin):
        success, message = services.change_pin(
            info.context.user, current_pin, new_pin, request=info.context
        )
        return ChangePin(success=success, message=message)


# ── Deprecated password-named aliases (existing Flutter client) ──────────────
# Thin wrappers over the same service functions; kept so current GraphQL
# clients that still send `password`/`changePassword`/etc. keep working.

class SendPasswordResetOtp(graphene.Mutation):
    """Deprecated alias of sendPinResetOtp."""
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)

    def mutate(self, info, email):
        try:
            message = services.send_reset_otp(email, request=info.context)
        except ratelimit.RateLimitExceeded:
            return SendPasswordResetOtp(success=False, message=services.GENERIC_RATE_LIMITED)
        return SendPasswordResetOtp(success=True, message=message)


class ResetPasswordWithOtp(graphene.Mutation):
    """Deprecated alias of resetPinWithOtp; accepts legacy `newPassword`."""
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        email = graphene.String(required=True)
        otp = graphene.String(required=True)
        new_password = graphene.String(required=True)

    def mutate(self, info, email, otp, new_password):
        try:
            success, message = services.reset_pin(email, otp, new_password, request=info.context)
        except ratelimit.RateLimitExceeded:
            return ResetPasswordWithOtp(success=False, message=services.GENERIC_RATE_LIMITED)
        return ResetPasswordWithOtp(success=success, message=message)


class ChangePassword(graphene.Mutation):
    """Deprecated alias of changePin; accepts legacy `currentPassword`/`newPassword`."""
    success = graphene.Boolean()
    message = graphene.String()

    class Arguments:
        current_password = graphene.String(required=True)
        new_password = graphene.String(required=True)

    @login_required
    def mutate(self, info, current_password, new_password):
        success, message = services.change_pin(
            info.context.user, current_password, new_password, request=info.context
        )
        return ChangePassword(success=success, message=message)


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
        # Returns only non-sensitive OTP metadata (see OTPType).
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

    # Canonical PIN mutations.
    send_pin_reset_otp = SendPinResetOtp.Field()
    reset_pin_with_otp = ResetPinWithOtp.Field()
    change_pin = ChangePin.Field()

    # Deprecated password-named aliases (backward compatibility).
    send_password_reset_otp = SendPasswordResetOtp.Field()
    reset_password_with_otp = ResetPasswordWithOtp.Field()
    change_password = ChangePassword.Field()

    token_auth = CustomObtainJSONWebToken.Field()
    verify_token = graphql_jwt.Verify.Field()
    refresh_token = graphql_jwt.Refresh.Field()
