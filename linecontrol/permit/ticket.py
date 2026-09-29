"""Permit ticket records, trips and equipment register."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..domain.events import utcnow


def _stamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        return datetime.fromisoformat(raw)
    return utcnow()


class TicketStatus:
    """Lifecycle states of a permit ticket."""

    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


#: Statuses that still let a ticket be exchanged for a release.
LIVE_STATUSES = frozenset({TicketStatus.ACTIVE})

#: Statuses the office sets itself; anything outside these is corrupt.
KNOWN_STATUSES = frozenset(
    {
        TicketStatus.ACTIVE,
        TicketStatus.USED,
        TicketStatus.REVOKED,
        TicketStatus.SUPERSEDED,
        TicketStatus.EXPIRED,
    }
)


@dataclass(frozen=True, slots=True)
class Ticket:
    """A time-boxed release ticket, valid for one trip on one generation."""

    ticket_id: str
    equipment: str
    generation: int
    trip_no: int
    applicant: str
    basis: str
    ttl_minutes: int
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    operator: str = ""
    status: str = TicketStatus.ACTIVE
    used_at: datetime | None = None
    used_by: str = ""
    revoked_reason: str = ""

    def is_live(self, now: datetime) -> bool:
        return self.status in LIVE_STATUSES and now < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "equipment": self.equipment,
            "generation": self.generation,
            "trip_no": self.trip_no,
            "applicant": self.applicant,
            "basis": self.basis,
            "ttl_minutes": self.ttl_minutes,
            "issued_by": self.issued_by,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "operator": self.operator,
            "status": self.status,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "used_by": self.used_by,
            "revoked_reason": self.revoked_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Ticket":
        used_at = raw.get("used_at")
        return cls(
            ticket_id=str(raw.get("ticket_id", "")),
            equipment=str(raw.get("equipment", "")),
            generation=int(raw.get("generation", 0)),
            trip_no=int(raw.get("trip_no", 0)),
            applicant=str(raw.get("applicant", "")),
            basis=str(raw.get("basis", "")),
            ttl_minutes=int(raw.get("ttl_minutes", 0)),
            issued_by=str(raw.get("issued_by", "")),
            issued_at=_stamp(raw.get("issued_at")),
            expires_at=_stamp(raw.get("expires_at")),
            operator=str(raw.get("operator", "")),
            status=str(raw.get("status", TicketStatus.ACTIVE)),
            used_at=_stamp(used_at) if used_at else None,
            used_by=str(raw.get("used_by", "")),
            revoked_reason=str(raw.get("revoked_reason", "")),
        )


@dataclass(frozen=True, slots=True)
class Trip:
    """An open equipment fault the ticket is allowed to reset."""

    equipment: str
    generation: int
    trip_no: int
    opened_at: datetime
    reason: str = ""
    open: bool = True
    closed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "equipment": self.equipment,
            "generation": self.generation,
            "trip_no": self.trip_no,
            "opened_at": self.opened_at.isoformat(),
            "reason": self.reason,
            "open": self.open,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Trip":
        closed_at = raw.get("closed_at")
        return cls(
            equipment=str(raw.get("equipment", "")),
            generation=int(raw.get("generation", 0)),
            trip_no=int(raw.get("trip_no", 0)),
            opened_at=_stamp(raw.get("opened_at")),
            reason=str(raw.get("reason", "")),
            open=bool(raw.get("open", True)),
            closed_at=_stamp(closed_at) if closed_at else None,
        )


@dataclass(slots=True)
class Equipment:
    """Generation counter for one protected piece of equipment."""

    equipment: str
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"equipment": self.equipment, "generation": self.generation}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Equipment":
        return cls(
            equipment=str(raw.get("equipment", "")),
            generation=int(raw.get("generation", 0)),
        )
