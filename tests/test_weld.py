"""Welder gating, splicing and the interlock latch."""

from __future__ import annotations

import pytest

from linecontrol.bus import EventBus
from linecontrol.domain.kinds import EventKind
from linecontrol.domain.records import Confirm, LatchSnapshot, utcnow
from linecontrol.store.filestore import FileStore
from linecontrol.weld.errors import WeldError, WeldErrorCode
from linecontrol.weld.state import WeldState
from linecontrol.weld.welder import Welder


class Aligner:
    """Stand-in for the entry section's confirmation lookup."""

    def __init__(self) -> None:
        self.confirm = Confirm(ok=False)

    def __call__(self) -> Confirm:
        return self.confirm


def build(tmp_path) -> tuple[Welder, Aligner]:
    aligner = Aligner()
    welder = Welder(aligner, FileStore(tmp_path), EventBus())
    return welder, aligner


def test_gate_refuses_without_a_confirmation(tmp_path) -> None:
    welder, _ = build(tmp_path)
    with pytest.raises(WeldError) as caught:
        welder.gate_arc()
    assert caught.value.code is WeldErrorCode.ALIGN_PENDING
    assert welder.state() == WeldState.IDLE


def test_arc_uses_the_confirmation_read_at_gate_time(tmp_path) -> None:
    welder, aligner = build(tmp_path)
    aligner.confirm = Confirm(ok=True, position=100.0, at=utcnow())
    welder.gate_arc()
    aligner.confirm = Confirm(ok=True, position=175.0, at=utcnow())
    welder.gate_arc()
    welder.arc()
    assert welder.track_point() == pytest.approx(175.0)
    assert welder.state() == WeldState.WELDING


def test_arc_is_blocked_while_the_latch_is_held(tmp_path) -> None:
    welder, aligner = build(tmp_path)
    aligner.confirm = Confirm(ok=True, position=100.0, at=utcnow())
    welder.alarm()
    welder.gate_arc()
    with pytest.raises(WeldError) as caught:
        welder.arc()
    assert caught.value.code is WeldErrorCode.LATCH_HELD
    ticket = welder.issue_permit("zhang.wei", "insulation resistance re-tested OK")
    welder.alarm_clear(ticket)
    welder.gate_arc()
    welder.arc()
    assert welder.interlock_ok()


def test_splice_writes_both_the_record_and_the_seam(tmp_path) -> None:
    welder, aligner = build(tmp_path)
    aligner.confirm = Confirm(ok=True, position=88.0, at=utcnow())
    welder.set_line_speed(1.5)
    welder.set_power(24.0, 300.0)
    welder.gate_arc()
    welder.arc()
    welder.persist_tension(90.0, 1.05)
    record = welder.splice()
    assert record.position == pytest.approx(88.0)
    assert record.heat_input == pytest.approx(24.0 * 300.0 / 1.5)
    assert welder.state() == WeldState.SPLICED
    assert welder.store.exists("weld.record")
    assert welder.store.exists("weld.seam." + record.seam_id)
    assert welder.seam_id() == record.seam_id


def test_splice_requires_a_running_arc(tmp_path) -> None:
    welder, _ = build(tmp_path)
    with pytest.raises(WeldError) as caught:
        welder.splice()
    assert caught.value.code is WeldErrorCode.ARC_BLOCKED


def test_power_validation(tmp_path) -> None:
    welder, _ = build(tmp_path)
    with pytest.raises(WeldError) as caught:
        welder.set_power(0.0, 10.0)
    assert caught.value.code is WeldErrorCode.POWER_INVALID
    welder.set_power(24.0, 300.0)
    assert welder.power_state() == (pytest.approx(24.0), pytest.approx(300.0))
    assert welder.heat_input() == 0.0


def test_latch_snapshot_round_trip(tmp_path) -> None:
    welder, _ = build(tmp_path)
    welder.alarm()
    assert welder.alarm_active() and not welder.interlock_ok()
    other = Welder(Aligner(), welder.store, EventBus())
    other.restore_latch(LatchSnapshot(interlock=True, alarm=True))
    assert other.alarm_active() and not other.interlock_ok()


def test_seam_history_is_newest_first(tmp_path) -> None:
    welder, aligner = build(tmp_path)
    aligner.confirm = Confirm(ok=True, position=10.0, at=utcnow())
    for position in (10.0, 20.0, 30.0):
        aligner.confirm = Confirm(ok=True, position=position, at=utcnow())
        welder.gate_arc()
        welder.arc()
        welder.splice()
    history = welder.seam_history(2)
    assert len(history) == 2
    assert history[0].at >= history[1].at


def test_arc_post_splice_regates_on_the_live_confirmation(tmp_path) -> None:
    welder, aligner = build(tmp_path)
    aligner.confirm = Confirm(ok=True, position=50.0, at=utcnow())
    welder.gate_arc()
    welder.arc()
    welder.splice()
    aligner.confirm = Confirm(ok=True, position=140.0, at=utcnow())
    welder.arc_post_splice()
    assert welder.track_point() == pytest.approx(140.0)


def test_snapshot_exposes_the_transition_history(tmp_path) -> None:
    welder, aligner = build(tmp_path)
    aligner.confirm = Confirm(ok=True, position=10.0, at=utcnow())
    welder.gate_arc()
    welder.arc()
    view = welder.snapshot()
    assert view["state"] == WeldState.WELDING
    assert view["transitions"] == [WeldState.IDLE, WeldState.GATED, WeldState.WELDING]


class FakeClock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.now = self.now + timedelta(seconds=seconds)


def build_clocked(tmp_path):
    aligner = Aligner()
    clock = FakeClock()
    welder = Welder(aligner, FileStore(tmp_path), EventBus(), clock=clock)
    return welder, aligner, clock


def test_clear_refuses_without_a_ticket(tmp_path) -> None:
    welder, _ = build(tmp_path)
    welder.alarm()
    with pytest.raises(WeldError) as caught:
        welder.alarm_clear()
    assert caught.value.code is WeldErrorCode.PERMIT_MISSING
    assert welder.interlock_ok() is False


def test_ticket_is_single_use(tmp_path) -> None:
    welder, _, _ = build_clocked(tmp_path)
    ticket = welder.issue_permit("li.qiang", "work order WO-441 signed off")
    welder.alarm()
    welder.alarm_clear(welder.validate_permit(ticket.ticket_id))
    assert welder.interlock_ok()
    welder.alarm()
    with pytest.raises(WeldError) as caught:
        welder.alarm_clear(welder.validate_permit(ticket.ticket_id))
    assert caught.value.code is WeldErrorCode.PERMIT_REVOKED


def test_ticket_expires_after_its_ttl(tmp_path) -> None:
    welder, _, clock = build_clocked(tmp_path)
    ticket = welder.issue_permit("li.qiang", "short maintenance window", ttl_seconds=30)
    clock.advance(31)
    welder.alarm()
    with pytest.raises(WeldError) as caught:
        welder.alarm_clear(welder.validate_permit(ticket.ticket_id))
    assert caught.value.code is WeldErrorCode.PERMIT_EXPIRED
    assert welder.interlock_ok() is False
    assert welder.permit_history()[0].status == "void"


def test_equipment_change_voids_outstanding_tickets(tmp_path) -> None:
    welder, _, _ = build_clocked(tmp_path)
    ticket = welder.issue_permit("li.qiang", "before the welder swap")
    assert welder.replace_equipment() == 1
    welder.alarm()
    with pytest.raises(WeldError) as caught:
        welder.alarm_clear(welder.validate_permit(ticket.ticket_id))
    assert caught.value.code is WeldErrorCode.PERMIT_STALE
    fresh = welder.issue_permit("wang.fang", "new welder acceptance test passed")
    welder.alarm_clear(welder.validate_permit(fresh.ticket_id))
    assert welder.interlock_ok()


def test_unknown_ticket_is_rejected(tmp_path) -> None:
    welder, _, _ = build_clocked(tmp_path)
    welder.alarm()
    with pytest.raises(WeldError) as caught:
        welder.alarm_clear(welder.validate_permit("does-not-exist"))
    assert caught.value.code is WeldErrorCode.PERMIT_MISSING


def test_issue_requires_applicant_and_basis(tmp_path) -> None:
    welder, _, _ = build_clocked(tmp_path)
    with pytest.raises(WeldError) as caught:
        welder.issue_permit("  ", "basis")
    assert caught.value.code is WeldErrorCode.PERMIT_MISSING
    with pytest.raises(WeldError) as caught:
        welder.issue_permit("li.qiang", "")
    assert caught.value.code is WeldErrorCode.PERMIT_MISSING


def test_ttl_is_capped_at_the_policy_ceiling(tmp_path) -> None:
    from linecontrol.weld.permit import MAX_TTL_SECONDS

    welder, _, _ = build_clocked(tmp_path)
    ticket = welder.issue_permit("li.qiang", "asking for too long", ttl_seconds=10**9)
    span = (ticket.expires_at - ticket.issued_at).total_seconds()
    assert span == pytest.approx(MAX_TTL_SECONDS)


def test_ticket_persists_across_restart(tmp_path) -> None:
    welder, _, _ = build_clocked(tmp_path)
    ticket = welder.issue_permit("li.qiang", "survive the restart")
    rebuilt = Welder(Aligner(), welder.store, EventBus(), clock=utcnow)
    validated = rebuilt.validate_permit(ticket.ticket_id)
    assert validated.applicant == "li.qiang"
    assert validated.basis == "survive the restart"


def test_permit_view_reports_validity_and_remaining_time(tmp_path) -> None:
    welder, _, clock = build_clocked(tmp_path)
    ticket = welder.issue_permit("li.qiang", "window", ttl_seconds=120)
    view = welder.permit_view()
    assert view["ticketId"] == ticket.ticket_id
    assert view["valid"] is True
    assert view["remainingSeconds"] == pytest.approx(120)
    clock.advance(121)
    assert welder.permit_view()["valid"] is False


def test_permit_events_identify_the_applicant(tmp_path) -> None:
    welder, _, _ = build_clocked(tmp_path)
    events = []
    welder.bus.subscribe(EventKind.PERMIT, events.append)
    ticket = welder.issue_permit("chen.ye", "WO-902")
    welder.alarm()
    welder.alarm_clear(welder.validate_permit(ticket.ticket_id))
    messages = [event.message for event in events]
    assert any("issued" in message and "chen.ye" in message for message in messages)
    assert any("consumed" in message and "WO-902" in message for message in messages)
