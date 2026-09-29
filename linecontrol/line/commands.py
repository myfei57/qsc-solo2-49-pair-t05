"""Single-step operator commands forwarded to the equipment areas."""

from __future__ import annotations

from ..domain.events import Event
from ..domain.kinds import EventKind
from ..domain.records import TensionSnapshot, WeldRecord
from ..pickle.dosing import DosePlan
from ..pump.errors import PumpError, PumpErrorCode
from ..tension.state import RampPlan

#: Alarm identifier raised for a welder overcurrent trip.
WELD_OVER_CURRENT = "weld-overcurrent"

#: Protected equipment id of the welder.
WELDER = "welder"


class CommandOps:
    """Mixed into :class:`~linecontrol.line.machine.Line`."""

    def recalibrate_tension(self, gain: float) -> bool:
        return self.tension.recalibrate(gain)

    def alarm_overcurrent(self) -> None:
        self.weld.alarm()
        if self.permits.open_trip_view(WELDER) is None:
            self.permits.open_trip(WELDER, reason="overcurrent")
        self.last_backup = self.store.backup()
        self.audit.raise_alarm(WELD_OVER_CURRENT)

    def clear_weld_alarm(self) -> bool:
        """Legacy clear with no ticket; tests and recovery only."""

        self.weld.alarm_clear()
        return self.audit.clear_alarm(WELD_OVER_CURRENT)

    def release_weld_latch(self, token: str, *, operator: str) -> dict[str, object]:
        """Exchange a valid ticket for the interlock release, then clear it."""

        ticket, trip = self.permits.redeem_ticket(WELDER, operator, token)
        self.weld.alarm_clear()
        self.audit.clear_alarm(WELD_OVER_CURRENT)
        return {
            "ticket": ticket.ticket_id,
            "trip": trip.trip_no,
            "applicant": ticket.applicant,
            "operator": ticket.used_by,
            "basis": ticket.basis,
            "issued_by": ticket.issued_by,
            "generation": ticket.generation,
        }

    def issue_weld_ticket(
        self,
        applicant: str,
        basis: str,
        *,
        issued_by: str,
        ttl_minutes: int = 120,
        operator: str = "",
    ) -> tuple[object, str]:
        return self.permits.issue_ticket(
            WELDER,
            applicant,
            basis,
            issued_by=issued_by,
            ttl_minutes=ttl_minutes,
            operator=operator,
        )

    def revoke_weld_ticket(self, ticket_id: str, *, revoked_by: str, reason: str) -> object:
        return self.permits.revoke_ticket(
            ticket_id, revoked_by=revoked_by, reason=reason
        )

    def replace_welder(self, *, actor: str, basis: str) -> int:
        """Record a welder replacement/retrofit and void every live ticket."""

        equipment = self.permits.replace_equipment(WELDER, actor=actor, basis=basis)
        self.weld.note_generation(equipment.generation)
        return equipment.generation

    def weld_tickets(self) -> tuple[object, ...]:
        return self.permits.list_tickets(WELDER)

    def splice_release(self) -> None:
        self.entry.splice_release()

    def track_head(self, delta: float) -> None:
        self.entry.track_head(delta)

    def align_offset(self, offset: float) -> None:
        self.entry.align_offset(offset)

    def correct_head(self) -> float:
        return self.entry.correct_head()

    def push_head_sample(self, value: float) -> None:
        self.entry.push_sample(value)

    def ramp_speed(self, target: float) -> RampPlan:
        plan = self.tension.plan_speedup(target)
        for step in plan.steps:
            self.tension.apply_step(step)
        return plan

    def replenish_acid(self, volume: float, target: float) -> DosePlan:
        plan = self.pickle.plan_dose(volume, target)
        if not plan.required:
            raise PumpError(
                PumpErrorCode.NO_REPLENISH,
                "concentration already at target",
            )
        self.pump.replenish_gate(target)
        self.pickle.apply_dose(plan.dose, volume)
        return plan

    def resume_acid_loop(self, sample: float) -> None:
        self.pickle.level_check(sample)
        self.pump.resume()

    def calibrate_acid(self, offset: float) -> None:
        self.pickle.calibrate(offset)

    def set_dryer_dew(self, dew: float) -> None:
        self.dry.set_dew_point(dew)

    def set_dryer_heat(self, target: float) -> float:
        next_temp = self.dry.heat_profile(target)
        self.dry.apply_profile(next_temp)
        return next_temp

    def reheat_dryer(self) -> None:
        self.dry.reheat()

    def set_weld_power(self, voltage: float, current: float) -> None:
        self.weld.set_power(voltage, current)

    def set_strip_speed(self, speed: float) -> None:
        self.strip.set_speed(speed)

    def advance_strip(self, delta: float) -> float:
        return self.strip.advance(delta)

    def set_shear_offset(self, offset: float) -> None:
        self.exit.set_shear_offset(offset)

    def taper_exit(self, base: float, diameter: float) -> float:
        return self.exit.taper_setpoint(base, diameter)

    def coil_weight(
        self,
        entry_thick: float,
        exit_thick: float,
        width: float,
        length: float,
    ) -> float:
        return self.exit.coil_weight(entry_thick, exit_thick, width, length)

    def sag(self) -> float:
        return self.strip.sag_check()

    def track_eta(self, target: float) -> float:
        return self.strip.eta(target)

    def persisted_tension(self) -> tuple[TensionSnapshot, bool]:
        return self.tension.persisted_tension()

    def weld_seams(self, limit: int = 0) -> list[WeldRecord]:
        return self.weld.seam_history(limit)

    def audit_query(self, kind: EventKind | None, limit: int = 0) -> list[Event]:
        if kind is None:
            return self.audit.recent(limit)
        return self.audit.query(kind, limit)
