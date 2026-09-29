"""In-memory register of actors, equipment, trips and tickets."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ticket import Equipment, Ticket, Trip

#: Permit-office capabilities.  An actor may hold more than one.
ROLE_SUPERVISOR = "supervisor"
ROLE_MAINTENANCE = "maintenance"
ROLE_OPERATOR = "operator"

KNOWN_ROLES = frozenset({ROLE_SUPERVISOR, ROLE_MAINTENANCE, ROLE_OPERATOR})

#: Reference identity directory seeded on first boot.  A site deployment must
#: replace this with its own people; the seeds keep a fresh instance usable.
SEED_ACTORS: dict[str, tuple[str, ...]] = {
    "u-supervisor": (ROLE_SUPERVISOR,),
    "u-maintenance": (ROLE_MAINTENANCE,),
    "u-operator": (ROLE_OPERATOR,),
}

#: Equipment protected by the permit office, seeded on first boot.
SEED_EQUIPMENT = ("welder",)

#: Default and hard bounds for a ticket's validity window, in minutes.
DEFAULT_TTL_MINUTES = 120
MAX_TTL_MINUTES = 480


@dataclass(slots=True)
class PermitState:
    actors: dict[str, frozenset[str]] = field(default_factory=dict)
    equipment: dict[str, Equipment] = field(default_factory=dict)
    trips: list[Trip] = field(default_factory=list)
    tickets: dict[str, Ticket] = field(default_factory=dict)
