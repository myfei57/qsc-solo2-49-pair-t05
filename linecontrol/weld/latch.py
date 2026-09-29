"""Overcurrent alarm and the interlock latch it leaves behind."""

from __future__ import annotations

from ..domain.events import Event
from ..domain.kinds import EventKind
from ..domain.records import LatchSnapshot, ReleaseTicket, utcnow
from ..store.keys import KEY_LATCH
from ..store.snapshot import save_json
from .errors import WeldError, WeldErrorCode


class LatchOps:
    """Mixed into :class:`~linecontrol.weld.welder.Welder`."""

    def alarm(self) -> None:
        self.data.alarm = True
        self.data.latch = True
        self._save_latch()
        self.bus.publish(
            Event(kind=EventKind.ALARM, source="weld", message="overcurrent")
        )

    def alarm_clear(self, ticket: ReleaseTicket | None = None) -> None:
        """Release the interlock against a validated, unconsumed ticket.

        The ticket is validated here as well as by the caller so the latch
        can never be released without an auditor-grade record of who
        authorised it and why.
        """

        if ticket is None:
            raise WeldError(
                WeldErrorCode.PERMIT_MISSING,
                "a valid release ticket is required to reset the interlock",
            )
        ticket = self.validate_permit(ticket.ticket_id)
        self.data.alarm = False
        self.data.latch = False
        self._save_latch()
        self.consume_permit(ticket)
        self.bus.publish(
            Event(
                kind=EventKind.LATCH,
                source="weld",
                message=(
                    f"interlock released by {ticket.applicant} "
                    f"permit={ticket.ticket_id}"
                ),
            )
        )

    def interlock_ok(self) -> bool:
        return not self.data.latch

    def alarm_active(self) -> bool:
        return self.data.alarm

    def restore_latch(self, snapshot: LatchSnapshot) -> None:
        self.data.latch = snapshot.interlock
        self.data.alarm = snapshot.alarm

    def _save_latch(self) -> None:
        save_json(
            self.store,
            KEY_LATCH,
            LatchSnapshot(
                interlock=self.data.latch,
                alarm=self.data.alarm,
                saved=utcnow(),
            ),
        )
