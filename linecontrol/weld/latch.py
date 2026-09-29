"""Overcurrent alarm and the interlock latch it leaves behind."""

from __future__ import annotations

from ..domain.events import Event
from ..domain.kinds import EventKind
from ..domain.records import LatchSnapshot, utcnow
from ..store.keys import KEY_LATCH
from ..store.snapshot import save_json


class LatchOps:
    """Mixed into :class:`~linecontrol.weld.welder.Welder`."""

    def alarm(self) -> None:
        self.data.alarm = True
        self.data.latch = True
        self._save_latch()
        self.bus.publish(
            Event(kind=EventKind.ALARM, source="weld", message="overcurrent")
        )

    def alarm_clear(self) -> None:
        """Release the hardware latch.

        This is the low-level primitive.  Production callers reach it through
        the permit office gate (``Line.release_weld_latch``), which refuses
        without a valid ticket; direct calls remain for tests and recovery.
        """

        self.data.alarm = False
        self.data.latch = False
        self._save_latch()
        self.bus.publish(
            Event(kind=EventKind.LATCH, source="weld", message="interlock released")
        )

    def interlock_ok(self) -> bool:
        return not self.data.latch

    def alarm_active(self) -> bool:
        return self.data.alarm

    def generation(self) -> int:
        return self.data.generation

    def note_generation(self, generation: int) -> None:
        """Adopt the permit office's current equipment generation."""

        if generation == self.data.generation:
            return
        self.data.generation = int(generation)
        self._save_latch()

    def restore_latch(self, snapshot: LatchSnapshot) -> None:
        self.data.latch = snapshot.interlock
        self.data.alarm = snapshot.alarm
        self.data.generation = snapshot.generation

    def _save_latch(self) -> None:
        save_json(
            self.store,
            KEY_LATCH,
            LatchSnapshot(
                interlock=self.data.latch,
                alarm=self.data.alarm,
                generation=self.data.generation,
                saved=utcnow(),
            ),
        )
