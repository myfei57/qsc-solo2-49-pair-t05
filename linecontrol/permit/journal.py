"""Append-only permit journal with a SHA-256 hash chain.

Every issuance, release, refusal and register change is one JSON line.  Each
entry carries the previous entry's hash, so deleting or rewriting an old line
breaks every link that follows and is detectable with :meth:`Journal.verify`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from ..domain.events import utcnow
from ..store.filestore import FileStore

#: Hash anchored before the first entry.
GENESIS = "0" * 64


def canonical(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class Journal:
    def __init__(self, store: FileStore, key: str) -> None:
        self._store = store
        self._key = key
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []

    def restore(self) -> None:
        raw = self._store.load(self._key)
        entries: list[dict[str, Any]] = []
        if raw:
            for line in raw.decode("utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ValueError("permit journal contains a corrupt line") from error
                    if not isinstance(entry, dict):
                        raise ValueError("permit journal contains a corrupt line")
                    entries.append(entry)
        self._entries = entries

    def append(
        self,
        action: str,
        actor: str,
        detail: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            entry: dict[str, Any] = {
                "seq": len(self._entries) + 1,
                "at": (at or utcnow()).isoformat(),
                "action": action,
                "actor": actor,
                "detail": dict(detail or {}),
                "prev": self._entries[-1]["hash"] if self._entries else GENESIS,
            }
            entry["hash"] = hashlib.sha256(canonical(entry)).hexdigest()
            line = canonical(entry).decode("utf-8") + "\n"
            self._store.append(self._key, line.encode("utf-8"))
            self._entries.append(entry)
            return dict(entry)

    def entries(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(entry) for entry in self._entries)

    def verify(self) -> bool:
        """Replay the chain from genesis and report any break."""

        previous = GENESIS
        for entry in self._entries:
            stored = entry.get("hash")
            if entry.get("prev") != previous:
                return False
            unsigned = {key: value for key, value in entry.items() if key != "hash"}
            if not isinstance(stored, str) or stored != hashlib.sha256(
                canonical(unsigned)
            ).hexdigest():
                return False
            previous = stored
        return True
