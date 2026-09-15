"""Single-prompt atomic snapshot manager for resume-from-interruption."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_FILENAME = "last_prompt_snapshot.json"


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    """Write JSON atomically (tmp file + fsync + os.replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


class SnapshotManager:
    """Atomic last-prompt snapshot: save before each driver call, resume after crash.

    The snapshot file layout follows SPEC v3 Section 4::

        {"turn_index": 5, "timestamp": "...Z",
         "request": {"messages": [...], "tools": [...]}}
    """

    def __init__(self, workspace_dir: str | Path, filename: str = SNAPSHOT_FILENAME) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.path = self.workspace_dir / filename

    def save(
        self,
        turn_index: int,
        request: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> Path:
        payload: dict[str, Any] = {
            "turn_index": turn_index,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "request": request,
        }
        if extra:
            payload.update(extra)
        return atomic_write_json(self.path, payload)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def has_snapshot(self) -> bool:
        return self.path.exists() and self.load() is not None

    def last_turn_index(self) -> int | None:
        snapshot = self.load()
        if not snapshot:
            return None
        try:
            return int(snapshot.get("turn_index", 0))
        except (TypeError, ValueError):
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
