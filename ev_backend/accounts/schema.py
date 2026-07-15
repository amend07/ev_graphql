import logging

import graphene
import graphql_jwt
from graphene_django import DjangoObjectType
from django.contrib.auth import get_user_model
from django.db.models import Q

from .permission import admin_required, login_required
from .models import AuditLog, PasswordResetOTP
from . import administration, services, ratelimit
from .auth_logging import log_event
from ev_backend.pagination import apply_ordering, hard_cap, paginate
# `stations.schema` imports `accounts.permission`, never `accounts.schema`, so
# this direction does not cycle. PublicUserType is the identity-only audience
# type shared by every "someone else's user" field in the API.
from stations.schema import PublicUserType, StationType

User = get_user_model()


class UserType(DjangoObjectType):
    """A user's own record, or a user as an administrator sees them.

    THE INVARIANT (B1 Phase 1): this type is only ever reachable from a root that
    already knows the caller is the subject or an admin — `me`, `tokenAuth`,
    `createUser` (returns the account just registered), `usersByRole`,
    `usersPage`, and the admin mutation payloads. It is deliberately NOT used for
    a station's owner or a review's author; those are `PublicUserType`.

    That invariant is what makes the fields below safe. Break it — hang this type
    off something public — and every one of them leaks. `test_user_type_exposure`
    pins the field list so an accidental widening fails loudly.

    `exclude` was the old approach and is why `is_superuser`, `password_reset_otp`
    and every reverse relation were public: it published each new field by
    default. An allow-list fails closed instead.
    """

    is_station_owner = graphene.Boolean()
    # Narrowed on purpose (B2.1): the reviewer is another admin, and this field
    # is readable by the owner they decided on via `me`. A rejected applicant
    # should learn that a decision was taken and by whom, not the deciding
    # admin's email address. Auto-conversion would have made this a full
    # `UserType` — the exact widening rule 3 exists to prevent.
    reviewer = graphene.Field(PublicUserType)

    class Meta:
        model = User
        fields = (
            # Identity — both clients select these.
            "id", "username", "email",
            # Authorization/state — clients route on role, W3 lists status.
            "role", "is_active", "owner_status",
            # Owner-decision provenance (B2.1). `rejectionReason` is what a
            # rejected owner is shown; `reviewedAt` is nullable because a
            # decision may never have been taken (or predates the field).
            "reviewed_at", "rejection_reason",
            # Administrative context, only reachable through the gated roots above.
            "is_staff", "is_superuser", "date_joined", "last_login",
        )
        # Return `role`/`owner_status` as raw lowercase values ("user",
        # "station_owner", "pending", …) rather than graphene-django's UPPERCASE
        # choice enums — both clients parse the lowercase strings.
        convert_choices_to_enum = False

    def resolve_is_station_owner(self, info):
        return self.role == "station_owner"

    def resolve_reviewer(self, info):
        return (
            PublicUserType(id=self.reviewer.id, username=self.reviewer.username)
            if self.reviewer_id else None
        )


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
    """Approve a pending station owner.

    Before B1 this set `is_active = True` on an account that was already active —
    a no-op, because registration never created anything pending. It now moves
    `owner_status` to approved, which is the flag `station_owner_required`
    actually gates on. Same name, same arguments, same payload: existing callers
    keep working and finally do something.
    """

    success = graphene.Boolean()
    user = graphene.Field(UserType)

    class Arguments:
        user_id = graphene.ID(required=True)

    @admin_required
    def mutate(self, info, user_id):
        target = administration.get_target(user_id)
        user = administration.approve_owner(
            actor=info.context.user, target=target, request=info.context,
        )
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


class DeletionSummaryType(graphene.ObjectType):
    """What deleting a user destroys, counted before it happens."""

    stations = graphene.Int(required=True)
    bookings = graphene.Int(required=True)
    reviews = graphene.Int(required=True)
    favorites = graphene.Int(required=True)
    other_users_affected = graphene.Int(
        required=True,
        description=(
            "Other people whose bookings or reviews are destroyed with this "
            "account, because they used a station it owns."
        ),
    )


class UserPage(graphene.ObjectType):
    items = graphene.List(UserType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


class AuditLogType(DjangoObjectType):
    class Meta:
        model = AuditLog
        fields = (
            "id", "actor_username", "action", "target_type", "target_id",
            "target_label", "metadata", "ip", "created_at",
        )
        convert_choices_to_enum = False


class AuditLogPage(graphene.ObjectType):
    items = graphene.List(AuditLogType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()


class DashboardSummaryType(graphene.ObjectType):
    """The admin dashboard, in one query and four aggregates.

    Every number here is computed by the database from data the platform
    actually holds. There is no revenue, no utilisation, no growth rate and no
    conversion figure, because `Booking` carries no money and no price snapshot:
    each of those would have to be fabricated, and a fabricated number on a
    dashboard is indistinguishable from a real one at a glance. See the sprint
    report for the list of what a future Payment model would unlock.
    """

    customers = graphene.Int(required=True)
    owners = graphene.Int(required=True)
    pending_owners = graphene.Int(
        required=True, description="Station owners awaiting an approval decision.",
    )
    rejected_owners = graphene.Int(required=True)

    stations = graphene.Int(required=True)
    active_stations = graphene.Int(required=True)
    inactive_stations = graphene.Int(required=True)

    bookings_today = graphene.Int(
        required=True,
        description=(
            "Bookings CREATED since local midnight — platform volume, not "
            "bookings whose slot is today. bookingsPage(dateFrom/dateTo) filters "
            "the slot instead."
        ),
    )
    bookings_this_week = graphene.Int(
        required=True, description="Bookings created since Monday 00:00 local.",
    )
    bookings_this_month = graphene.Int(
        required=True, description="Bookings created since the 1st, 00:00 local.",
    )

    reviews = graphene.Int(
        required=True, description="Visible reviews. Hidden ones are excluded.",
    )
    average_rating = graphene.Float(
        required=True, description="Mean of visible review ratings; 0.0 when there are none.",
    )

    newest_users = graphene.List(graphene.NonNull(UserType), required=True)
    newest_stations = graphene.List(graphene.NonNull(StationType), required=True)


# Ordering allow-list for usersPage. A free-form order_by would let a caller sort
# by `password` and read the hash out one comparison at a time.
USER_ORDER_FIELDS = {
    'newest': '-date_joined',
    'oldest': 'date_joined',
    'username': 'username',
    'last_login': '-last_login',
}

# Same discipline for the audit trail (B2.1).
AUDIT_ORDER_FIELDS = {
    'newest': '-created_at',
    'oldest': 'created_at',
    'action': 'action',
}


class AdminQuery(graphene.ObjectType):
    users_by_role = graphene.List(
        UserType,
        role=graphene.String(required=True),
        description=(
            "DEPRECATED — use usersPage. Kept for the existing web client. "
            "Bounded by GRAPHQL_LIST_HARD_CAP; it cannot page or search."
        ),
    )
    users_page = graphene.Field(
        UserPage,
        role=graphene.String(),
        search=graphene.String(),
        is_active=graphene.Boolean(),
        owner_status=graphene.String(),
        order_by=graphene.String(),
        limit=graphene.Int(),
        offset=graphene.Int(),
        # Says what `search` and `orderBy` actually do. Without this a client can
        # only guess, and an honest client is then forced to write vague UI copy
        # rather than name the fields it searches — which is exactly what
        # happened in W4. The B2.1 endpoints document theirs; this one is B1's
        # and did not.
        description=(
            "Paginated, searchable, filterable user directory. Admin only. "
            "search matches username or email (case-insensitive, partial). "
            "orderBy is an allow-list (newest | oldest | username | last_login); "
            "an unrecognised value falls back to newest rather than erroring."
        ),
    )
    all_otps = graphene.List(OTPType)
    audit_logs_page = graphene.Field(
        AuditLogPage,
        actor_id=graphene.ID(),
        action=graphene.String(),
        target_type=graphene.String(),
        target_id=graphene.String(),
        date_from=graphene.DateTime(),
        date_to=graphene.DateTime(),
        order_by=graphene.String(),
        limit=graphene.Int(),
        offset=graphene.Int(),
        description=(
            "The administrative audit trail. All filters are additive since B1: "
            "actorId, targetType, dateFrom, dateTo and orderBy are new in B2.1."
        ),
    )
    user_deletion_preview = graphene.Field(
        DeletionSummaryType,
        user_id=graphene.ID(required=True),
        description="What deleting this user would destroy. Changes nothing.",
    )
    dashboard_summary = graphene.Field(
        DashboardSummaryType,
        newest_limit=graphene.Int(
            description="How many recent users/stations to include (1–20, default 5).",
        ),
        description="Platform totals for the admin dashboard, in one query.",
    )

    @admin_required
    def resolve_users_by_role(self, info, role):
        # Bounded (B1 Phase 6): this was the one collection that could return the
        # whole table. The cap is a truncation the caller cannot see, which is
        # why it is deprecated in favour of usersPage rather than left as-is.
        return hard_cap(User.objects.filter(role=role).order_by('id'))

    @admin_required
    def resolve_users_page(self, info, role=None, search=None, is_active=None,
                           owner_status=None, order_by=None, limit=None, offset=None):
        qs = User.objects.all()

        if role:
            qs = qs.filter(role=role)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if owner_status:
            qs = qs.filter(owner_status=owner_status)
        if search:
            term = search.strip()
            if term:
                qs = qs.filter(
                    Q(username__icontains=term) | Q(email__icontains=term)
                )

        qs = apply_ordering(qs, order_by, USER_ORDER_FIELDS, '-date_joined')

        items, total, has_next = paginate(qs, limit, offset)
        return UserPage(items=items, total_count=total, has_next=has_next)

    @admin_required
    def resolve_all_otps(self, info):
        # Returns only non-sensitive OTP metadata (see OTPType).
        return hard_cap(PasswordResetOTP.objects.order_by('-created_at'))

    @admin_required
    def resolve_audit_logs_page(self, info, actor_id=None, action=None,
                                target_type=None, target_id=None, date_from=None,
                                date_to=None, order_by=None, limit=None, offset=None):
        qs = AuditLog.objects.all()
        if actor_id:
            # Matches the FK, which is SET_NULL: entries whose actor has since
            # been deleted are unreachable by this filter by construction. They
            # are still in the trail, and `actorUsername` still names who acted —
            # filter on `search`-less pages or by date to find them.
            qs = qs.filter(actor_id=actor_id)
        if action:
            qs = qs.filter(action=action)
        if target_type:
            qs = qs.filter(target_type=target_type)
        if target_id:
            qs = qs.filter(target_id=str(target_id))
        if date_from is not None:
            qs = qs.filter(created_at__gte=date_from)
        if date_to is not None:
            qs = qs.filter(created_at__lte=date_to)

        # Ties on -id, not id: within one timestamp the newest entry stays first.
        qs = apply_ordering(qs, order_by, AUDIT_ORDER_FIELDS, '-created_at', tie_break='-id')

        items, total, has_next = paginate(qs, limit, offset)
        return AuditLogPage(items=items, total_count=total, has_next=has_next)

    @admin_required
    def resolve_user_deletion_preview(self, info, user_id):
        return DeletionSummaryType(
            **administration.deletion_summary(administration.get_target(user_id))
        )

    @admin_required
    def resolve_dashboard_summary(self, info, newest_limit=None):
        return DashboardSummaryType(
            **administration.platform_summary(newest_limit=newest_limit)
        )


class DeleteUser(graphene.Mutation):
    ok = graphene.Boolean()
    summary = graphene.Field(
        DeletionSummaryType,
        description="What this delete destroyed. Additive since B1.",
    )

    class Arguments:
        user_id = graphene.ID(required=True)

    @admin_required
    def mutate(self, info, user_id):
        target = administration.get_target(user_id)
        summary = administration.delete_user(
            actor=info.context.user, target=target, request=info.context,
        )
        return DeleteUser(ok=True, summary=DeletionSummaryType(**summary))


class ToggleUserActive(graphene.Mutation):
    user = graphene.Field(UserType)

    class Arguments:
        user_id = graphene.ID(required=True)
        is_active = graphene.Boolean(required=True)

    @admin_required
    def mutate(self, info, user_id, is_active):
        target = administration.get_target(user_id)
        user = administration.set_user_active(
            actor=info.context.user, target=target, is_active=is_active,
            request=info.context,
        )
        return ToggleUserActive(user=user)


class RejectStationOwner(graphene.Mutation):
    """Reject a station owner's application. The reason is now mandatory.

    `reason` moved from `String` to `String!` in B2.1. That is a breaking change
    to the argument's type and is made deliberately, while it is still free: no
    shipped client calls this mutation yet (B1 built it; W3 has not adopted it),
    and the web client in flight has been told to always send one. A rejection
    nobody can account for is worse than a rejection that is slightly harder to
    submit — the applicant has to be told something.
    """

    success = graphene.Boolean()
    user = graphene.Field(UserType)

    class Arguments:
        user_id = graphene.ID(required=True)
        reason = graphene.String(
            required=True,
            description="Why the application was rejected. Shown to the applicant.",
        )

    @admin_required
    def mutate(self, info, user_id, reason):
        target = administration.get_target(user_id)
        user = administration.reject_owner(
            actor=info.context.user, target=target, reason=reason,
            request=info.context,
        )
        return RejectStationOwner(success=True, user=user)


class AdminMutation(graphene.ObjectType):
    delete_user = DeleteUser.Field()
    toggle_user_active = ToggleUserActive.Field()
    reject_station_owner = RejectStationOwner.Field()


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
