"""Single-prompt atomic snapshot manager for resume-from-interruption."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SNAPSHOT_FILENAME = "last_prompt_snapshot.json"


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    """Write JSON atomically (tmp file + os.replace)."""
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


@contextmanager
def exclusive_file_lock(path: str | Path, timeout: float = 60.0) -> Iterator[None]:
    """Cross-process exclusive lock (Windows ``msvcrt`` / POSIX ``fcntl``).

    Serialises read-modify-write of shared files such as ``leaderboard.json``.
    ``os.replace`` alone is atomic per write, but two processes can still
    load the same snapshot and the later replace drops the other's merge.
    """
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(timeout, 0.1)
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out locking %s" % lock_path) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


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


def self_test() -> tuple[int, int]:
    import tempfile
    import threading

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} snapshot::{name}", flush=True)

    with tempfile.TemporaryDirectory(prefix="snap-lock-") as tmp:
        board = Path(tmp) / "board.json"
        atomic_write_json(board, {})
        errors: list[str] = []

        def merge(key: str) -> None:
            try:
                with exclusive_file_lock(board, timeout=10.0):
                    data = json.loads(board.read_text(encoding="utf-8"))
                    time.sleep(0.05)
                    data[key] = True
                    atomic_write_json(board, data)
            except Exception as exc:
                errors.append("%s: %r" % (key, exc))

        t1 = threading.Thread(target=merge, args=("a",))
        t2 = threading.Thread(target=merge, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        final = json.loads(board.read_text(encoding="utf-8"))
        check("lock_no_errors", errors == [])
        check("lock_keeps_both_keys", final.get("a") is True and final.get("b") is True)
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
