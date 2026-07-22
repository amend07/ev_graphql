"""Charging-session business logic — the one place sessions are created,
metered, and ended, so the ``cost = energy × price`` invariant and the
booking-to-done transition cannot be forgotten by a caller.
"""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from bookings.models import Booking
from stations.models import Station

from .gateway import ChargerError, get_charger_gateway
from .models import ChargingSession


class ChargingError(Exception):
    """Caller-visible problem starting/stopping a session."""


def _resolve_booking(user, station, booking_id):
    """The approved booking that authorises charging here, or None if walk-up is
    allowed. Raises if the caller has no standing to use this charger."""
    approved = Booking.objects.filter(
        user=user, station=station, status='approved',
    )
    if booking_id:
        booking = approved.filter(id=booking_id).first()
        if booking is None:
            raise ChargingError("No approved booking for this charger.")
        return booking
    booking = approved.order_by('start_time', 'id').first()
    if booking is None and not getattr(settings, 'CHARGING_ALLOW_WALKUP', False):
        raise ChargingError(
            "You need an approved booking to start charging at this station."
        )
    return booking


@transaction.atomic
def start_session(*, user, charger_code, booking_id=None):
    station = Station.objects.filter(
        charger_code=charger_code, is_active=True,
    ).first()
    if station is None:
        raise ChargingError("Unknown or inactive charger.")

    if ChargingSession.objects.filter(
        user=user, status=ChargingSession.STATUS_ACTIVE,
    ).exists():
        raise ChargingError("You already have an active charging session.")

    if ChargingSession.objects.filter(
        charger_code=charger_code, status=ChargingSession.STATUS_ACTIVE,
    ).exists():
        raise ChargingError("This charger is currently in use.")

    booking = _resolve_booking(user, station, booking_id)

    gateway = get_charger_gateway()
    try:
        gateway.start(station=station, charger_code=charger_code)
    except ChargerError as e:
        raise ChargingError(str(e) or "The charger could not be started.")

    try:
        session = ChargingSession.objects.create(
            user=user, station=station, booking=booking,
            charger_code=charger_code,
            price_per_kwh=float(station.price_per_kwh),
            status=ChargingSession.STATUS_ACTIVE,
            energy_delivered_kwh=0.0,
        )
    except IntegrityError:
        # Lost a race for the per-user / per-charger active-session constraint.
        raise ChargingError("A charging session is already active here.")
    return session


def _apply(session, reading):
    session.energy_delivered_kwh = round(max(0.0, reading.energy_kwh), 3)
    if reading.battery_percent is not None:
        session.battery_percent = round(reading.battery_percent, 1)


def _finalize(session, *, status, end_reason):
    session.status = status
    session.end_reason = end_reason
    session.ended_at = timezone.now()
    session.save()
    # Keep booking history consistent: a finished session marks its booking done.
    # This complements the owner's manual "mark as done" — either path is valid.
    booking = session.booking
    if booking is not None and booking.status == 'approved':
        booking.status = 'done'
        booking.save(update_fields=['status'])


def refresh_session(session):
    """Poll the charger and reconcile the session. Auto-ends on full. Called on
    every read so the client never has to ask the backend to end a session."""
    if session.status != ChargingSession.STATUS_ACTIVE:
        return session
    reading = get_charger_gateway().read(session)
    _apply(session, reading)
    if reading.status != ChargingSession.STATUS_ACTIVE:
        _finalize(session, status=reading.status, end_reason=reading.end_reason)
    else:
        session.save(update_fields=['energy_delivered_kwh', 'battery_percent'])
    return session


def stop_session(session):
    """Customer-initiated stop. Idempotent: stopping an ended session is a no-op."""
    if session.status != ChargingSession.STATUS_ACTIVE:
        return session
    reading = get_charger_gateway().stop(session)
    _apply(session, reading)
    _finalize(
        session, status=ChargingSession.STATUS_COMPLETED,
        end_reason=ChargingSession.REASON_STOPPED,
    )
    return session


def report_interruption(session):
    """Charger fault / unplug / power loss: end with the partial metered cost.
    Invoked by a real gateway's fault handler (and directly by tests)."""
    if session.status != ChargingSession.STATUS_ACTIVE:
        return session
    reading = get_charger_gateway().read(session)
    _apply(session, reading)
    _finalize(
        session, status=ChargingSession.STATUS_INTERRUPTED,
        end_reason=ChargingSession.REASON_INTERRUPTED,
    )
    return session


def active_session_for(user):
    return ChargingSession.objects.filter(
        user=user, status=ChargingSession.STATUS_ACTIVE,
    ).select_related('station').first()
