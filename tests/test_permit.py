"""Permit-to-reset ticket office: issue, gate, expire, supersede, audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from linecontrol.bus import EventBus
from linecontrol.permit.errors import PermitError, PermitErrorCode
from linecontrol.permit.office import PermitOffice
from linecontrol.permit.ticket import TicketStatus
from linecontrol.store.filestore import FileStore

EQUIPMENT = "welder"
SUPERVISOR = "u-supervisor"
MAINTENANCE = "u-maintenance"
OPERATOR = "u-operator"
BASIS = "WO-2026-091 feeder short cleared; insulation replaced"


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))


@pytest.fixture()
def office(tmp_path, clock: FakeClock) -> PermitOffice:
    return PermitOffice(FileStore(tmp_path / "permit"), EventBus(), clock=clock)


def trip_ticket(office: PermitOffice, **overrides: object):
    if office.open_trip_view(EQUIPMENT) is None:
        office.open_trip(EQUIPMENT, reason="overcurrent")
    kwargs: dict[str, object] = {
        "issued_by": SUPERVISOR,
        "ttl_minutes": 120,
        "operator": OPERATOR,
    }
    kwargs.update(overrides)
    return office.issue_ticket(EQUIPMENT, OPERATOR, BASIS, **kwargs)  # type: ignore[arg-type]


def test_happy_path_releases_and_consumes_the_ticket(
    office: PermitOffice, clock: FakeClock
) -> None:
    ticket, token = trip_ticket(office)
    assert ticket.status == TicketStatus.ACTIVE
    assert ticket.expires_at == clock.now + timedelta(minutes=120)  # noqa: SLF001
    assert office.open_trip_view(EQUIPMENT) is not None

    used, trip = office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert used.status == TicketStatus.USED
    assert used.used_by == OPERATOR
    assert trip.open is False
    assert office.open_trip_view(EQUIPMENT) is None
    assert office.ledger_intact() is True


def test_release_without_a_ticket_is_impossible(office: PermitOffice) -> None:
    # Even with an open trip, the latch gate only accepts well-formed tickets.
    office.open_trip(EQUIPMENT)
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, "not-a-ticket")
    assert caught.value.code is PermitErrorCode.BAD_TICKET
    actions = [entry["action"] for entry in office.ledger()]
    assert "release.denied" in actions


def test_unknown_or_unprivileged_actor_is_refused(office: PermitOffice) -> None:
    with pytest.raises(PermitError) as caught:
        office.open_trip(EQUIPMENT)
        office.issue_ticket(EQUIPMENT, OPERATOR, BASIS, issued_by="nobody")
    assert caught.value.code is PermitErrorCode.UNKNOWN_ACTOR

    with pytest.raises(PermitError) as caught:
        office.issue_ticket(EQUIPMENT, OPERATOR, BASIS, issued_by=OPERATOR)
    assert caught.value.code is PermitErrorCode.FORBIDDEN


def test_tampered_signature_is_rejected(office: PermitOffice) -> None:
    _, token = trip_ticket(office)
    body, tag = token.split(".")
    forged = body + "." + tag[:-2] + ("aa" if not tag.endswith("aa") else "bb")
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, forged)
    assert caught.value.code is PermitErrorCode.BAD_SIGNATURE
    assert office.get_ticket(office.list_tickets()[0].ticket_id).status == TicketStatus.ACTIVE


def test_ticket_expires_after_its_window(office: PermitOffice, clock: FakeClock) -> None:
    _, token = trip_ticket(office, ttl_minutes=30)
    clock.advance(31)
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert caught.value.code is PermitErrorCode.TICKET_EXPIRED
    assert office.list_tickets()[0].status == TicketStatus.EXPIRED


def test_equipment_generation_change_voids_live_tickets(
    office: PermitOffice, clock: FakeClock
) -> None:
    ticket, token = trip_ticket(office)
    equipment = office.replace_equipment(
        EQUIPMENT, actor=MAINTENANCE, basis="welder power module R2 retrofit"
    )
    assert equipment.generation == 1
    assert office.open_trip_view(EQUIPMENT) is None
    assert office.get_ticket(ticket.ticket_id).status == TicketStatus.SUPERSEDED
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert caught.value.code is PermitErrorCode.TICKET_SUPERSEDED

    # A fresh ticket issued after replacement works on the new generation.
    office.open_trip(EQUIPMENT)
    new_ticket, new_token = trip_ticket(office)
    assert new_ticket.generation == 1
    used, _ = office.redeem_ticket(EQUIPMENT, OPERATOR, new_token)
    assert used.status == TicketStatus.USED

def test_ticket_is_single_use(office: PermitOffice) -> None:
    _, token = trip_ticket(office)
    office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    # The same ticket cannot reset a later trip on the same equipment.
    office.open_trip(EQUIPMENT)
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert caught.value.code is PermitErrorCode.TICKET_WRONG_TRIP


def test_revoked_ticket_is_refused(office: PermitOffice) -> None:
    ticket, token = trip_ticket(office)
    office.revoke_ticket(ticket.ticket_id, revoked_by=SUPERVISOR, reason="fault not cleared")
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert caught.value.code is PermitErrorCode.TICKET_REVOKED


def test_ticket_names_its_operator(office: PermitOffice) -> None:
    # A non-operator can never present a ticket...
    _, token = trip_ticket(office)
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, SUPERVISOR, token)
    assert caught.value.code is PermitErrorCode.FORBIDDEN

    # ...and even another operator cannot use a ticket not naming them.
    office._state.actors["u-operator-2"] = frozenset({"operator"})
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, "u-operator-2", token)
    assert caught.value.code is PermitErrorCode.TICKET_WRONG_OPERATOR


def test_ticket_for_another_trip_is_refused(office: PermitOffice) -> None:
    _, token = trip_ticket(office)
    office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    office.open_trip(EQUIPMENT, reason="overcurrent again")
    with pytest.raises(PermitError) as caught:
        office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert caught.value.code is PermitErrorCode.TICKET_WRONG_TRIP


def test_issue_requires_an_open_trip_and_a_basis(office: PermitOffice) -> None:
    with pytest.raises(PermitError) as caught:
        office.issue_ticket(EQUIPMENT, OPERATOR, BASIS, issued_by=SUPERVISOR)
    assert caught.value.code is PermitErrorCode.TRIP_NOT_FOUND

    office.open_trip(EQUIPMENT)
    with pytest.raises(PermitError) as caught:
        office.issue_ticket(EQUIPMENT, OPERATOR, "   ", issued_by=SUPERVISOR)
    assert caught.value.code is PermitErrorCode.BASIS_REQUIRED

    with pytest.raises(PermitError) as caught:
        office.issue_ticket(
            EQUIPMENT, OPERATOR, BASIS, issued_by=SUPERVISOR, ttl_minutes=0
        )
    assert caught.value.code is PermitErrorCode.TTL_INVALID


def test_journal_records_who_and_why(office: PermitOffice) -> None:
    ticket, token = trip_ticket(office)
    office.redeem_ticket(EQUIPMENT, OPERATOR, token)
    actions = [(entry["action"], entry["actor"]) for entry in office.ledger()]
    assert ("trip.opened", "system") in actions
    assert ("ticket.issued", SUPERVISOR) in actions
    released = [entry for entry in office.ledger() if entry["action"] == "ticket.released"][0]
    assert released["actor"] == OPERATOR
    assert released["detail"]["ticket"] == ticket.ticket_id
    assert released["detail"]["basis"] == BASIS


def test_journal_tampering_is_detected_on_reload(tmp_path, clock: FakeClock) -> None:
    directory = tmp_path / "permit"
    first = PermitOffice(FileStore(directory), EventBus(), clock=clock)
    trip_ticket(first)
    journal_file = directory / "permit.journal"
    lines = journal_file.read_text(encoding="utf-8").splitlines()
    import json as json_module

    broken = json_module.loads(lines[0])
    broken["detail"]["reason"] = "rewritten history"
    lines[0] = json_module.dumps(broken, ensure_ascii=False)
    journal_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from linecontrol.permit.errors import PermitError as PermitFailure

    with pytest.raises(PermitFailure) as caught:
        PermitOffice(FileStore(directory), EventBus(), clock=clock)
    assert caught.value.code is PermitErrorCode.JOURNAL_TAMPERED


def test_state_survives_restart(tmp_path, clock: FakeClock) -> None:
    directory = tmp_path / "permit"
    first = PermitOffice(FileStore(directory), EventBus(), clock=clock)
    ticket, token = trip_ticket(first)
    clock.advance(10)

    restarted = PermitOffice(FileStore(directory), EventBus(), clock=clock)
    assert restarted.generation(EQUIPMENT) == 0
    assert restarted.open_trip_view(EQUIPMENT).trip_no == 1
    assert restarted.ledger_intact() is True
    used, _ = restarted.redeem_ticket(EQUIPMENT, OPERATOR, token)
    assert used.ticket_id == ticket.ticket_id
    assert used.status == TicketStatus.USED
