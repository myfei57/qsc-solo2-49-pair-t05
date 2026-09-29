"""Permit-to-reset failures."""

from __future__ import annotations

from enum import Enum


class PermitErrorCode(str, Enum):
    UNKNOWN_ACTOR = "unknown-actor"
    FORBIDDEN = "forbidden"
    TRIP_NOT_FOUND = "trip-not-found"
    TRIP_ALREADY_OPEN = "trip-already-open"
    EQUIPMENT_GENERATION_INVALID = "equipment-generation-invalid"
    BASIS_REQUIRED = "basis-required"
    TTL_INVALID = "ttl-invalid"
    BAD_TICKET = "bad-ticket"
    BAD_SIGNATURE = "bad-signature"
    TICKET_NOT_FOUND = "ticket-not-found"
    TICKET_WRONG_EQUIPMENT = "ticket-wrong-equipment"
    TICKET_WRONG_TRIP = "ticket-wrong-trip"
    TICKET_WRONG_OPERATOR = "ticket-wrong-operator"
    TICKET_EXPIRED = "ticket-expired"
    TICKET_USED = "ticket-used"
    TICKET_REVOKED = "ticket-revoked"
    TICKET_SUPERSEDED = "ticket-superseded"
    JOURNAL_TAMPERED = "journal-tampered"


class PermitError(RuntimeError):
    """A refusal from the permit office; never opens the interlock by itself."""

    def __init__(self, code: PermitErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}
