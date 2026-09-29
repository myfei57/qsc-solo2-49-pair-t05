"""Time-limited, single-use tickets that authorise releasing the latch.

A ticket names the applicant and the basis for the reset, carries an expiry
timestamp and is bound to the welder's current equipment generation. The
latch cannot be released without presenting a ticket that is found on disk,
unexpired, unconsumed and issued against the equipment currently in place.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..domain.events import Event
from ..domain.kinds import EventKind
from ..domain.records import (
    TICKET_CONSUMED,
    TICKET_ISSUED,
    TICKET_VOID,
    ReleaseTicket,
)
from ..store.keys import KEY_GENERATION, KEY_TICKET, TICKET_PREFIX
from ..store.snapshot import load_float, load_json, save_float, save_json
from .errors import WeldError, WeldErrorCode

#: Default lifetime granted to a freshly issued ticket, in seconds.
DEFAULT_TTL_SECONDS = 1800.0

#: Policy ceiling for a ticket lifetime, in seconds.
MAX_TTL_SECONDS = 1800.0


def _aware(stamp: datetime) -> datetime:
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


class PermitOps:
    """Mixed into :class:`~linecontrol.weld.welder.Welder`."""

    def issue_permit(
        self,
        applicant: str,
        basis: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> ReleaseTicket:
        applicant = applicant.strip()
        basis = basis.strip()
        if not applicant:
            raise WeldError(WeldErrorCode.PERMIT_MISSING, "applicant is required")
        if not basis:
            raise WeldError(WeldErrorCode.PERMIT_MISSING, "release basis is required")
        ttl = float(ttl_seconds)
        if ttl <= 0:
            raise WeldError(WeldErrorCode.PERMIT_EXPIRED, "ticket lifetime must be positive")
        ttl = min(ttl, MAX_TTL_SECONDS)

        now = _aware(self._clock())
        ticket = ReleaseTicket(
            ticket_id=uuid4().hex,
            applicant=applicant,
            basis=basis,
            generation=self.equipment_generation(),
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        self._save_ticket(ticket)
        self.bus.publish(
            Event(
                kind=EventKind.PERMIT,
                source="weld",
                message=f"issued:{ticket.ticket_id} applicant={applicant} basis={basis}",
                payload=float(ticket.generation),
            )
        )
        return ticket

    def validate_permit(self, ticket_id: str) -> ReleaseTicket:
        """Return the ticket only if it may release the latch right now."""

        if not ticket_id:
            raise WeldError(
                WeldErrorCode.PERMIT_MISSING, "no release ticket presented"
            )
        ticket, ok = load_json(self.store, TICKET_PREFIX + ticket_id, ReleaseTicket)
        if not ok or ticket.ticket_id != ticket_id:
            raise WeldError(
                WeldErrorCode.PERMIT_MISSING, f"ticket not found: {ticket_id}"
            )
        if ticket.generation != self.equipment_generation():
            raise WeldError(
                WeldErrorCode.PERMIT_STALE,
                f"ticket {ticket_id} predates the current equipment generation",
            )
        if ticket.status == TICKET_CONSUMED:
            raise WeldError(
                WeldErrorCode.PERMIT_REVOKED, f"ticket {ticket_id} already consumed"
            )
        if ticket.status == TICKET_VOID:
            raise WeldError(WeldErrorCode.PERMIT_REVOKED, f"ticket {ticket_id} is void")
        if _aware(self._clock()) >= _aware(ticket.expires_at):
            self._save_ticket(replace(ticket, status=TICKET_VOID))
            self.bus.publish(
                Event(
                    kind=EventKind.PERMIT,
                    source="weld",
                    message=f"expired:{ticket_id}",
                    payload=float(ticket.generation),
                )
            )
            raise WeldError(
                WeldErrorCode.PERMIT_EXPIRED, f"ticket {ticket_id} has expired"
            )
        return ticket

    def consume_permit(self, ticket: ReleaseTicket) -> None:
        consumed = replace(
            ticket,
            status=TICKET_CONSUMED,
            consumed_at=_aware(self._clock()),
        )
        self._save_ticket(consumed)
        self.bus.publish(
            Event(
                kind=EventKind.PERMIT,
                source="weld",
                message=(
                    f"consumed:{consumed.ticket_id} "
                    f"applicant={consumed.applicant} basis={consumed.basis}"
                ),
                payload=float(consumed.generation),
            )
        )

    def replace_equipment(self) -> int:
        """Bump the equipment generation and void every outstanding ticket."""

        generation = self.equipment_generation() + 1
        save_float(self.store, KEY_GENERATION, float(generation))
        for key in self.store.list(TICKET_PREFIX):
            ticket, ok = load_json(self.store, key, ReleaseTicket)
            if ok and ticket.status == TICKET_ISSUED:
                self._save_ticket(replace(ticket, status=TICKET_VOID))
                self.bus.publish(
                    Event(
                        kind=EventKind.PERMIT,
                        source="weld",
                        message=f"void:{ticket.ticket_id}",
                        payload=float(generation),
                    )
                )
        self.bus.publish(
            Event(
                kind=EventKind.PERMIT,
                source="weld",
                message=f"equipment-generation:{generation}",
                payload=float(generation),
            )
        )
        return generation

    def equipment_generation(self) -> int:
        return int(load_float(self.store, KEY_GENERATION))

    def current_permit(self) -> tuple[ReleaseTicket, bool]:
        """The most recently issued ticket, whether or not it is still usable."""

        return load_json(self.store, KEY_TICKET, ReleaseTicket)

    def permit_view(self) -> dict[str, object] | None:
        ticket, ok = self.current_permit()
        if not ok:
            return None
        now = _aware(self._clock())
        valid = (
            ticket.status == TICKET_ISSUED
            and ticket.generation == self.equipment_generation()
            and now < _aware(ticket.expires_at)
        )
        return {
            **ticket.to_dict(),
            "valid": valid,
            "remainingSeconds": max((_aware(ticket.expires_at) - now).total_seconds(), 0.0),
        }

    def permit_history(self, limit: int = 0) -> list[ReleaseTicket]:
        records: list[ReleaseTicket] = []
        for key in self.store.list(TICKET_PREFIX):
            ticket, ok = load_json(self.store, key, ReleaseTicket)
            if ok:
                records.append(ticket)
        records.sort(key=lambda item: item.issued_at, reverse=True)
        if limit > 0:
            return records[:limit]
        return records

    def _save_ticket(self, ticket: ReleaseTicket) -> None:
        save_json(self.store, TICKET_PREFIX + ticket.ticket_id, ticket)
        save_json(self.store, KEY_TICKET, ticket)
