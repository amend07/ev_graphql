import graphene

from accounts.permission import active_required, login_required

from . import service
from .models import ChargingSession


class ChargingSessionType(graphene.ObjectType):
    """A metered session, named to the client contract (sessionId/stationId),
    hand-rolled so it exposes exactly these fields and never the user FK."""

    session_id = graphene.ID(required=True)
    booking_id = graphene.ID()
    station_id = graphene.ID(required=True)
    charger_code = graphene.String(required=True)
    # active | completed | interrupted (String, matching the booking-status
    # convention elsewhere in this schema).
    status = graphene.String(required=True)
    started_at = graphene.DateTime(required=True)
    ended_at = graphene.DateTime()
    energy_delivered_kwh = graphene.Float(required=True)
    price_per_kwh = graphene.Float(required=True)
    cost = graphene.Float(required=True)
    battery_percent = graphene.Float()
    end_reason = graphene.String()


def _session_type(s):
    return ChargingSessionType(
        session_id=s.id,
        booking_id=s.booking_id,
        station_id=s.station_id,
        charger_code=s.charger_code,
        status=s.status,
        started_at=s.started_at,
        ended_at=s.ended_at,
        energy_delivered_kwh=s.energy_delivered_kwh,
        price_per_kwh=s.price_per_kwh,
        cost=s.cost,
        battery_percent=s.battery_percent,
        end_reason=s.end_reason or None,
    )


def _owned_session(user, session_id):
    session = ChargingSession.objects.filter(
        id=session_id, user=user,
    ).select_related('station', 'booking').first()
    if session is None:
        raise Exception("Charging session not found.")
    return session


class ChargingQuery(graphene.ObjectType):
    charging_session = graphene.Field(
        ChargingSessionType, session_id=graphene.ID(required=True),
        description="Live meter reading for your session. Poll ~5s while active; "
                    "auto-ends on full/interruption with no further client call.",
    )
    my_active_charging_session = graphene.Field(
        ChargingSessionType,
        description="Your current active session, if any — lets the app resume "
                    "the live screen after being reopened.",
    )

    @login_required
    def resolve_charging_session(self, info, session_id):
        session = _owned_session(info.context.user, session_id)
        session = service.refresh_session(session)  # advance meter, maybe auto-end
        return _session_type(session)

    @login_required
    def resolve_my_active_charging_session(self, info):
        session = service.active_session_for(info.context.user)
        if session is None:
            return None
        session = service.refresh_session(session)
        # A refresh may have auto-ended it; only surface it if still active.
        if session.status != ChargingSession.STATUS_ACTIVE:
            return None
        return _session_type(session)


class StartChargingSession(graphene.Mutation):
    session = graphene.Field(ChargingSessionType)

    class Arguments:
        charger_code = graphene.String(required=True)
        booking_id = graphene.ID()

    @active_required
    def mutate(self, info, charger_code, booking_id=None):
        try:
            session = service.start_session(
                user=info.context.user,
                charger_code=charger_code,
                booking_id=booking_id,
            )
        except service.ChargingError as e:
            raise Exception(str(e))
        return StartChargingSession(session=_session_type(session))


class StopChargingSession(graphene.Mutation):
    session = graphene.Field(ChargingSessionType)

    class Arguments:
        session_id = graphene.ID(required=True)

    @active_required
    def mutate(self, info, session_id):
        session = _owned_session(info.context.user, session_id)
        session = service.stop_session(session)
        return StopChargingSession(session=_session_type(session))


class ChargingMutation(graphene.ObjectType):
    start_charging_session = StartChargingSession.Field()
    stop_charging_session = StopChargingSession.Field()
