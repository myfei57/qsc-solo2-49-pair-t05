"""Permit office: who may ask for a time-boxed interlock release.

The gate rule is deliberately simple and fail-closed: clearing the welder
latch needs a signed ticket whose equipment, generation and trip all match
the machine standing here right now, whose named operator presents it, and
which has neither expired nor been used, revoked or superseded by an
equipment generation change.  Every attempt, allowed or refused, lands in
the hash-chained journal.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import NoReturn
from uuid import uuid4

from ..bus import EventBus
from ..domain.events import Event
from ..domain.events import utcnow as default_clock
from ..domain.kinds import EventKind
from ..store.filestore import FileStore
from ..store.keys import (
    KEY_PERMIT_JOURNAL,
    KEY_PERMIT_SECRET,
    KEY_PERMIT_STATE,
    TICKET_PREFIX,
    TRIP_PREFIX,
)
from .errors import PermitError, PermitErrorCode
from .journal import Journal
from .state import (
    DEFAULT_TTL_MINUTES,
    MAX_TTL_MINUTES,
    ROLE_MAINTENANCE,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    SEED_ACTORS,
    SEED_EQUIPMENT,
    PermitState,
)
from .ticket import KNOWN_STATUSES, Equipment, Ticket, TicketStatus, Trip
from .token import TOKEN_VERSION, issue_token, stamp, verify_token

#: Journal action vocabulary.
ACT_TRIP_OPENED = "trip.opened"
ACT_TRIP_CLOSED = "trip.closed"
ACT_TICKET_ISSUED = "ticket.issued"
ACT_TICKET_RELEASED = "ticket.released"
ACT_TICKET_REVOKED = "ticket.revoked"
ACT_TICKET_EXPIRED = "ticket.expired"
ACT_TICKET_SUPERSEDED = "ticket.superseded"
ACT_GENERATION_BUMPED = "generation.bumped"
ACT_RELEASE_DENIED = "release.denied"

#: Refusals journaled under ``release.denied`` and mirrored to the bus.
_DENIED = {
    PermitErrorCode.UNKNOWN_ACTOR,
    PermitErrorCode.FORBIDDEN,
    PermitErrorCode.BAD_TICKET,
    PermitErrorCode.BAD_SIGNATURE,
    PermitErrorCode.TICKET_NOT_FOUND,
    PermitErrorCode.TICKET_WRONG_EQUIPMENT,
    PermitErrorCode.TICKET_WRONG_TRIP,
    PermitErrorCode.TICKET_WRONG_OPERATOR,
    PermitErrorCode.TICKET_EXPIRED,
    PermitErrorCode.TICKET_USED,
    PermitErrorCode.TICKET_REVOKED,
    PermitErrorCode.TICKET_SUPERSEDED,
}

_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class PermitOffice:
    """Owns the ticket register and the release decision."""

    def __init__(
        self,
        store: FileStore,
        bus: EventBus,
        clock: Callable[[], datetime] = default_clock,
    ) -> None:
        self._store = store
        self._bus = bus
        self._clock = clock
        self._lock = threading.RLock()
        self._state = PermitState()
        self._secret = b""
        self.journal = Journal(store, KEY_PERMIT_JOURNAL)
        self._load_or_bootstrap()

    # -- bootstrap and persistence -------------------------------------

    def _load_or_bootstrap(self) -> None:
        raw = self._store.load(KEY_PERMIT_SECRET)
        if raw is None:
            self._secret = os.urandom(32)
            self._store.save(KEY_PERMIT_SECRET, self._secret)
            try:
                os.chmod(self._store.directory_path(KEY_PERMIT_SECRET), 0o600)
            except OSError:
                pass
            for actor, roles in SEED_ACTORS.items():
                self._state.actors[actor] = frozenset(roles)
            for name in SEED_EQUIPMENT:
                self._state.equipment[name] = Equipment(name, 0)
            self._save_register()
        else:
            self._secret = raw
            register = self._load_register()
            self._state.actors = {
                actor: frozenset(roles)
                for actor, roles in register.get("actors", SEED_ACTORS).items()
            }
            for name, value in register.get("equipment", {}).items():
                self._state.equipment[name] = Equipment(name, int(value))
            self._restore_records()
        try:
            self.journal.restore()
        except ValueError as error:
            raise PermitError(
                PermitErrorCode.JOURNAL_TAMPERED,
                "permit journal is unreadable; releases stay locked",
            ) from error
        if not self.journal.verify():
            raise PermitError(
                PermitErrorCode.JOURNAL_TAMPERED,
                "permit journal hash chain is broken; releases stay locked",
            )

    def _save_register(self) -> None:
        payload = {
            "actors": {actor: sorted(roles) for actor, roles in self._state.actors.items()},
            "equipment": {
                name: item.generation for name, item in self._state.equipment.items()
            },
        }
        self._store.save(
            KEY_PERMIT_STATE, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def _load_register(self) -> dict:
        raw = self._store.load(KEY_PERMIT_STATE)
        if raw is None:
            return {"actors": dict(SEED_ACTORS), "equipment": {name: 0 for name in SEED_EQUIPMENT}}
        return json.loads(raw.decode("utf-8"))

    def _restore_records(self) -> None:
        for name in self._store.list(TRIP_PREFIX):
            raw = self._store.load(name)
            if raw:
                trip = Trip.from_dict(json.loads(raw.decode("utf-8")))
                self._state.trips.append(trip)
        self._state.trips.sort(key=lambda item: item.trip_no)
        for name in self._store.list(TICKET_PREFIX):
            raw = self._store.load(name)
            if raw:
                ticket = Ticket.from_dict(json.loads(raw.decode("utf-8")))
                self._state.tickets[ticket.ticket_id] = ticket

    # -- identity and equipment ----------------------------------------

    def _actor_roles(self, actor: str) -> frozenset[str]:
        roles = self._state.actors.get(actor)
        if roles is None:
            raise PermitError(PermitErrorCode.UNKNOWN_ACTOR, f"unknown actor {actor!r}")
        return roles

    def _require_role(self, actor: str, role: str) -> frozenset[str]:
        roles = self._actor_roles(actor)
        if role not in roles:
            raise PermitError(
                PermitErrorCode.FORBIDDEN,
                f"{actor!r} lacks {role!r}",
            )
        return roles

    def register_equipment(self, name: str) -> Equipment:
        with self._lock:
            if not _ID_SAFE.match(name):
                raise PermitError(
                    PermitErrorCode.EQUIPMENT_GENERATION_INVALID,
                    f"invalid equipment id {name!r}",
                )
            existing = self._state.equipment.get(name)
            if existing is not None:
                return existing
            equipment = Equipment(name, 0)
            self._state.equipment[name] = equipment
            self._save_register()
            return equipment

    def generation(self, equipment: str) -> int:
        with self._lock:
            item = self._require_equipment(equipment)
            return item.generation

    def _require_equipment(self, equipment: str) -> Equipment:
        item = self._state.equipment.get(equipment)
        if item is None:
            raise PermitError(
                PermitErrorCode.TICKET_WRONG_EQUIPMENT,
                f"no protected equipment named {equipment!r}",
            )
        return item

    # -- trips -----------------------------------------------------------

    def open_trip(
        self,
        equipment: str,
        reason: str = "",
        at: datetime | None = None,
        actor: str = "system",
    ) -> Trip:
        """Record the overcurrent trip a ticket must name."""

        with self._lock:
            item = self._require_equipment(equipment)
            open_trip = self.find_open_trip(equipment)
            if open_trip is not None:
                raise PermitError(
                    PermitErrorCode.TRIP_ALREADY_OPEN,
                    f"{equipment} trip {open_trip.trip_no} is still open",
                )
            trip_no = len(self._state.trips) + 1
            trip = Trip(
                equipment=equipment,
                generation=item.generation,
                trip_no=trip_no,
                opened_at=at or self._clock(),
                reason=reason,
            )
            self._state.trips.append(trip)
            self._save_trip(trip)
            self._log(ACT_TRIP_OPENED, actor, {"trip": trip.trip_no, "reason": reason})
            return trip

    def _close_trip(self, trip: Trip, at: datetime, actor: str = "system") -> Trip:
        closed = Trip(
            equipment=trip.equipment,
            generation=trip.generation,
            trip_no=trip.trip_no,
            opened_at=trip.opened_at,
            reason=trip.reason,
            open=False,
            closed_at=at,
        )
        for index, existing in enumerate(self._state.trips):
            if existing.equipment == trip.equipment and existing.trip_no == trip.trip_no:
                self._state.trips[index] = closed
                break
        self._save_trip(closed)
        self._log(ACT_TRIP_CLOSED, actor, {"trip": closed.trip_no})
        return closed

    def find_open_trip(self, equipment: str) -> Trip | None:
        for trip in reversed(self._state.trips):
            if trip.equipment == equipment and trip.open:
                return trip
        return None

    def _save_trip(self, trip: Trip) -> None:
        self._store.save(f"{TRIP_PREFIX}{trip.equipment}.{trip.trip_no}", _json_bytes(trip.to_dict()))

    # -- tickets ---------------------------------------------------------

    def issue_ticket(
        self,
        equipment: str,
        applicant: str,
        basis: str,
        *,
        issued_by: str,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
        operator: str = "",
    ) -> tuple[Ticket, str]:
        """Supervisor issues a time-boxed ticket for the open trip."""

        with self._lock:
            self._require_role(issued_by, ROLE_SUPERVISOR)
            self._actor_roles(applicant)
            operator = operator or applicant
            self._require_role(operator, ROLE_OPERATOR)
            if not basis or not basis.strip():
                raise PermitError(PermitErrorCode.BASIS_REQUIRED, "ticket needs a written basis")
            ttl = int(ttl_minutes)
            if not 0 < ttl <= MAX_TTL_MINUTES:
                raise PermitError(
                    PermitErrorCode.TTL_INVALID,
                    f"ttl must be within 1..{MAX_TTL_MINUTES} minutes",
                )
            item = self._require_equipment(equipment)
            trip = self.find_open_trip(equipment)
            if trip is None:
                raise PermitError(
                    PermitErrorCode.TRIP_NOT_FOUND,
                    f"{equipment} has no open trip to release",
                )
            self._sweep_expired()
            now = self._clock()
            ticket = Ticket(
                ticket_id="PT-" + uuid4().hex[:12],
                equipment=equipment,
                generation=item.generation,
                trip_no=trip.trip_no,
                applicant=applicant,
                basis=basis.strip(),
                ttl_minutes=ttl,
                issued_by=issued_by,
                issued_at=now,
                expires_at=now + timedelta(minutes=ttl),
                operator=operator,
            )
            self._state.tickets[ticket.ticket_id] = ticket
            self._save_ticket(ticket)
            token = self._encode(ticket)
            self._log(
                ACT_TICKET_ISSUED,
                issued_by,
                {
                    "ticket": ticket.ticket_id,
                    "equipment": ticket.equipment,
                    "generation": ticket.generation,
                    "trip": ticket.trip_no,
                    "applicant": ticket.applicant,
                    "operator": ticket.operator,
                    "basis": ticket.basis,
                    "ttl_minutes": ticket.ttl_minutes,
                    "expires_at": stamp(ticket.expires_at),
                },
            )
            return ticket, token

    def redeem_ticket(self, equipment: str, actor: str, token: str) -> tuple[Ticket, Trip]:
        """Validate and single-use consume a ticket; nothing here clears hardware."""

        with self._lock:
            def deny(code: PermitErrorCode, detail: str) -> NoReturn:
                self._log(
                    ACT_RELEASE_DENIED,
                    actor,
                    {"equipment": equipment, "reason": code.value, "detail": detail},
                )
                raise PermitError(code, detail)

            try:
                self._require_role(actor, ROLE_OPERATOR)
            except PermitError as error:
                if error.code in _DENIED:
                    deny(error.code, error.detail)
                raise

            payload = self._decode(token, deny)
            ticket_id = str(payload.get("tid", ""))
            ticket = self._state.tickets.get(ticket_id)
            if ticket is None:
                deny(PermitErrorCode.TICKET_NOT_FOUND, f"no ticket {ticket_id!r}")

            if ticket.equipment != equipment:
                deny(
                    PermitErrorCode.TICKET_WRONG_EQUIPMENT,
                    f"ticket is for {ticket.equipment!r}, not {equipment!r}",
                )
            item = self._require_equipment(equipment)
            trip = self.find_open_trip(equipment)
            if ticket.generation != item.generation:
                self._set_status(ticket, TicketStatus.SUPERSEDED)
                self._log(
                    ACT_TICKET_SUPERSEDED,
                    actor,
                    {
                        "ticket": ticket.ticket_id,
                        "generation": ticket.generation,
                        "current_generation": item.generation,
                    },
                )
                deny(
                    PermitErrorCode.TICKET_SUPERSEDED,
                    "equipment changed after the ticket was issued",
                )
            if trip is None:
                deny(PermitErrorCode.TRIP_NOT_FOUND, f"{equipment} has no open trip")
            if ticket.trip_no != trip.trip_no:
                deny(
                    PermitErrorCode.TICKET_WRONG_TRIP,
                    f"ticket names trip {ticket.trip_no}, open trip is {trip.trip_no}",
                )
            if ticket.operator and ticket.operator != actor:
                deny(
                    PermitErrorCode.TICKET_WRONG_OPERATOR,
                    f"ticket names operator {ticket.operator!r}",
                )
            now = self._clock()
            if now >= ticket.expires_at:
                self._set_status(ticket, TicketStatus.EXPIRED)
                self._log(
                    ACT_TICKET_EXPIRED,
                    actor,
                    {"ticket": ticket.ticket_id, "expires_at": stamp(ticket.expires_at)},
                )
                deny(PermitErrorCode.TICKET_EXPIRED, "ticket validity window has passed")
            if ticket.status != TicketStatus.ACTIVE:
                deny(
                    {
                        TicketStatus.USED: PermitErrorCode.TICKET_USED,
                        TicketStatus.REVOKED: PermitErrorCode.TICKET_REVOKED,
                        TicketStatus.SUPERSEDED: PermitErrorCode.TICKET_SUPERSEDED,
                        TicketStatus.EXPIRED: PermitErrorCode.TICKET_EXPIRED,
                    }.get(ticket.status, PermitErrorCode.BAD_TICKET),
                    f"ticket is {ticket.status}",
                )

            used = Ticket(
                ticket_id=ticket.ticket_id,
                equipment=ticket.equipment,
                generation=ticket.generation,
                trip_no=ticket.trip_no,
                applicant=ticket.applicant,
                basis=ticket.basis,
                ttl_minutes=ticket.ttl_minutes,
                issued_by=ticket.issued_by,
                issued_at=ticket.issued_at,
                expires_at=ticket.expires_at,
                operator=ticket.operator,
                status=TicketStatus.USED,
                used_at=now,
                used_by=actor,
                revoked_reason=ticket.revoked_reason,
            )
            self._state.tickets[ticket.ticket_id] = used
            self._save_ticket(used)
            closed_trip = self._close_trip(trip, now, actor=actor)
            self._log(
                ACT_TICKET_RELEASED,
                actor,
                {
                    "ticket": ticket.ticket_id,
                    "equipment": ticket.equipment,
                    "generation": ticket.generation,
                    "trip": ticket.trip_no,
                    "applicant": ticket.applicant,
                    "basis": ticket.basis,
                },
            )
            self._bus.publish(
                Event(
                    kind=EventKind.PERMIT,
                    source="permit",
                    message="release",
                    payload=float(ticket.trip_no),
                )
            )
            return used, closed_trip

    def revoke_ticket(self, ticket_id: str, *, revoked_by: str, reason: str) -> Ticket:
        with self._lock:
            self._require_role(revoked_by, ROLE_SUPERVISOR)
            if not reason or not reason.strip():
                raise PermitError(PermitErrorCode.BASIS_REQUIRED, "revocation needs a reason")
            ticket = self._state.tickets.get(ticket_id)
            if ticket is None:
                raise PermitError(PermitErrorCode.TICKET_NOT_FOUND, f"no ticket {ticket_id!r}")
            if ticket.status != TicketStatus.ACTIVE:
                raise PermitError(
                    PermitErrorCode.BAD_TICKET,
                    f"ticket is already {ticket.status}",
                )
            revoked = _replace(ticket, status=TicketStatus.REVOKED, revoked_reason=reason.strip())
            self._state.tickets[ticket_id] = revoked
            self._save_ticket(revoked)
            self._log(
                ACT_TICKET_REVOKED,
                revoked_by,
                {"ticket": ticket_id, "reason": reason.strip()},
            )
            return revoked

    def replace_equipment(
        self, equipment: str, *, actor: str, basis: str, new_generation: int | None = None
    ) -> Equipment:
        """Maintenance records a replacement/retrofit, voiding every live ticket."""

        with self._lock:
            self._require_role(actor, ROLE_MAINTENANCE)
            if not basis or not basis.strip():
                raise PermitError(PermitErrorCode.BASIS_REQUIRED, "replacement needs a basis")
            item = self._require_equipment(equipment)
            next_generation = item.generation + 1 if new_generation is None else int(new_generation)
            if next_generation <= item.generation:
                raise PermitError(
                    PermitErrorCode.EQUIPMENT_GENERATION_INVALID,
                    f"new generation {next_generation} must exceed {item.generation}",
                )
            updated = Equipment(equipment, next_generation)
            self._state.equipment[equipment] = updated
            self._save_register()
            open_trip = self.find_open_trip(equipment)
            if open_trip is not None:
                self._close_trip(open_trip, self._clock(), actor=actor)
            for ticket in self._state.tickets.values():
                if ticket.equipment == equipment and ticket.status == TicketStatus.ACTIVE:
                    superseded = _replace(ticket, status=TicketStatus.SUPERSEDED)
                    self._state.tickets[ticket.ticket_id] = superseded
                    self._save_ticket(superseded)
                    self._log(
                        ACT_TICKET_SUPERSEDED,
                        actor,
                        {
                            "ticket": ticket.ticket_id,
                            "generation": ticket.generation,
                            "current_generation": next_generation,
                            "reason": "equipment replacement",
                        },
                    )
            self._log(
                ACT_GENERATION_BUMPED,
                actor,
                {
                    "equipment": equipment,
                    "generation": item.generation,
                    "current_generation": next_generation,
                    "basis": basis.strip(),
                },
            )
            return updated

    # -- reads -----------------------------------------------------------

    def get_ticket(self, ticket_id: str) -> Ticket:
        with self._lock:
            ticket = self._state.tickets.get(ticket_id)
            if ticket is None:
                raise PermitError(PermitErrorCode.TICKET_NOT_FOUND, f"no ticket {ticket_id!r}")
            return ticket

    def list_tickets(self, equipment: str = "") -> tuple[Ticket, ...]:
        with self._lock:
            self._sweep_expired()
            tickets = self._state.tickets.values()
            if equipment:
                tickets = [ticket for ticket in tickets if ticket.equipment == equipment]
            return tuple(sorted(tickets, key=lambda item: item.issued_at, reverse=True))

    def open_trip_view(self, equipment: str) -> Trip | None:
        with self._lock:
            return self.find_open_trip(equipment)

    def equipment_view(self) -> tuple[Equipment, ...]:
        with self._lock:
            return tuple(sorted(self._state.equipment.values(), key=lambda item: item.equipment))

    def ledger(self) -> tuple[dict, ...]:
        return self.journal.entries()

    def ledger_intact(self) -> bool:
        return self.journal.verify()

    # -- helpers ---------------------------------------------------------

    def _sweep_expired(self) -> int:
        now = self._clock()
        count = 0
        for ticket in list(self._state.tickets.values()):
            if ticket.status == TicketStatus.ACTIVE and now >= ticket.expires_at:
                expired = _replace(ticket, status=TicketStatus.EXPIRED)
                self._state.tickets[ticket.ticket_id] = expired
                self._save_ticket(expired)
                self._log(
                    ACT_TICKET_EXPIRED,
                    "system",
                    {"ticket": ticket.ticket_id, "expires_at": stamp(ticket.expires_at)},
                )
                count += 1
        return count

    def _set_status(self, ticket: Ticket, status: str) -> None:
        self._state.tickets[ticket.ticket_id] = _replace(ticket, status=status)
        self._save_ticket(self._state.tickets[ticket.ticket_id])

    def _save_ticket(self, ticket: Ticket) -> None:
        if ticket.status not in KNOWN_STATUSES:
            raise PermitError(PermitErrorCode.BAD_TICKET, f"unknown status {ticket.status!r}")
        self._store.save(f"{TICKET_PREFIX}{ticket.ticket_id}", _json_bytes(ticket.to_dict()))

    def _encode(self, ticket: Ticket) -> str:
        payload = {
            "v": TOKEN_VERSION,
            "tid": ticket.ticket_id,
            "eq": ticket.equipment,
            "gen": ticket.generation,
            "trip": ticket.trip_no,
            "op": ticket.operator,
            "app": ticket.applicant,
            "iat": stamp(ticket.issued_at),
            "exp": stamp(ticket.expires_at),
        }
        return issue_token(self._secret, payload)

    def _decode(self, token: str, deny: Callable[[PermitErrorCode, str], None]) -> dict:
        try:
            return verify_token(self._secret, token)
        except PermitError as error:
            deny(error.code, error.detail)

    def _log(self, action: str, actor: str, detail: dict[str, object]) -> dict:
        entry = self.journal.append(action, actor, detail)
        self._bus.publish(
            Event(
                kind=EventKind.PERMIT,
                source="permit",
                message=action,
                payload=float(entry["seq"]),
            )
        )
        return entry


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _replace(ticket: Ticket, **changes: object) -> Ticket:
    data = ticket.to_dict()
    for field_name, value in changes.items():
        if field_name in {"issued_at", "expires_at", "used_at"}:
            continue
        data[field_name] = value
    return Ticket.from_dict(data)
