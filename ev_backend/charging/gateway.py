"""The seam between the platform and the physical charger.

Everything that would, in production, be an OCPP command or a meter read against
real hardware goes through :class:`ChargerGateway`. The default
:class:`SimulatedChargerGateway` derives a plausible meter reading from elapsed
time and the station's rated power, so the whole session flow — start, live
polling, auto-complete on full — is real and testable with no hardware.

Swapping in a real integration is a settings change (``CHARGER_GATEWAY``) plus a
subclass, exactly as ``accounts.sms`` swaps SMS backends. Nothing above this file
knows whether a charger is real.
"""

from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from .models import ChargingSession


@dataclass(frozen=True)
class Reading:
    """A meter reading, plus whether the charger says the session should end."""

    energy_kwh: float
    battery_percent: float | None
    status: str            # ChargingSession.STATUS_*
    end_reason: str | None


class ChargerError(Exception):
    """The charger refused to start (offline, occupied, no cable)."""


class ChargerGateway:
    def start(self, *, station, charger_code):  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, session):  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self, session):  # pragma: no cover - interface
        raise NotImplementedError


class SimulatedChargerGateway(ChargerGateway):
    """A deterministic charger: delivers the station's rated power until a
    simulated full charge, then reports completion. No randomness (a test must be
    able to assert an exact cost), and it never fabricates an interruption — a
    real fault is reported by real hardware; tests drive that path explicitly via
    ``service.report_interruption``.
    """

    def _full_seconds(self):
        return getattr(settings, 'CHARGING_SIM_FULL_SECONDS', 3600)

    def _reading(self, session, *, now=None):
        now = now or timezone.now()
        full_seconds = self._full_seconds()
        elapsed = max(0.0, (now - session.started_at).total_seconds())
        capped = min(elapsed, full_seconds)
        power_kw = session.station.power_output_kw
        energy = power_kw * (capped / 3600.0)
        battery = 100.0 * (capped / full_seconds) if full_seconds else 100.0
        if elapsed >= full_seconds:
            return Reading(energy, 100.0, ChargingSession.STATUS_COMPLETED,
                           ChargingSession.REASON_FULL)
        return Reading(energy, round(battery, 1), ChargingSession.STATUS_ACTIVE, None)

    def start(self, *, station, charger_code):
        # A real gateway rejects an offline/occupied charger here; the simulator
        # accepts (occupancy is already enforced by the active-session constraint).
        return None

    def read(self, session):
        return self._reading(session)

    def stop(self, session):
        # Final reading at stop time; the caller stamps stopped_by_user.
        r = self._reading(session)
        return Reading(r.energy_kwh, r.battery_percent,
                       ChargingSession.STATUS_ACTIVE, None)


_GATEWAY = None


def get_charger_gateway():
    """Resolve the configured gateway (cached). Mirrors ``get_sms_backend``."""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = SimulatedChargerGateway()
    return _GATEWAY
