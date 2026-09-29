"""Permit-to-reset: signed, time-boxed tickets gating the welder latch."""

from __future__ import annotations

from .errors import PermitError, PermitErrorCode
from .office import PermitOffice
from .state import (
    ROLE_MAINTENANCE,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    DEFAULT_TTL_MINUTES,
    MAX_TTL_MINUTES,
)
from .ticket import Ticket, TicketStatus, Trip

__all__ = [
    "DEFAULT_TTL_MINUTES",
    "MAX_TTL_MINUTES",
    "PermitError",
    "PermitErrorCode",
    "PermitOffice",
    "ROLE_MAINTENANCE",
    "ROLE_OPERATOR",
    "ROLE_SUPERVISOR",
    "Ticket",
    "TicketStatus",
    "Trip",
]
