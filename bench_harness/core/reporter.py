"""Terminal ANSI live status rendering + ``live_status.json`` sidecar."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.snapshot import atomic_write_json

ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "running": "\033[36m",  # cyan
    "ok": "\033[32m",  # green
    "passed": "\033[32m",
    "failed": "\033[31m",  # red
    "error": "\033[31m",
    "retry": "\033[33m",  # yellow
    "info": "\033[37m",
}


class ProgressReporter:
    """Live multi-task progress: ANSI status lines plus atomic sidecar file.

    Parameters
    ----------
    status_path:
        Destination of the ``live_status.json`` sidecar (atomically updated).
    enabled:
        When False, suppress terminal rendering (keeps sidecar writes).
    stream:
        Text stream for rendering (defaults to stdout).
    """

    def __init__(
        self,
        status_path: str | Path,
        enabled: bool = True,
        stream: Any | None = None,
    ) -> None:
        self.status_path = Path(status_path)
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stdout
        self._state: dict[str, Any] = {
            "session": None,
            "tasks": {},
            "updated_at": None,
        }

    # -- public API --------------------------------------------------------

    def start_session(self, session_id: str, total_tasks: int = 0) -> None:
        self._state["session"] = {"session_id": session_id, "total_tasks": total_tasks}
        self._touch()
        self._render("info", session_id, "session started")

    def update(
        self,
        task_id: str,
        status: str = "running",
        message: str = "",
        turn: int | None = None,
        tokens: int | None = None,
    ) -> None:
        entry: dict[str, Any] = {"status": status, "message": message}
        if turn is not None:
            entry["turn"] = turn
        if tokens is not None:
            entry["tokens"] = tokens
        self._state["tasks"][task_id] = entry
        self._touch()
        self._render(status, task_id, message, turn=turn, tokens=tokens)

    def complete_task(self, task_id: str, passed: bool, message: str = "") -> None:
        self.update(task_id, "passed" if passed else "failed", message or ("passed" if passed else "failed"))

    def finish(self, message: str = "done") -> None:
        self._touch()
        self._write_sidecar()
        if self.enabled:
            self.stream.write(f"{ANSI_COLORS['ok']}[bench] {message}{ANSI_RESET}\n")
            self.stream.flush()

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self._state["session"],
            "tasks": dict(self._state["tasks"]),
            "updated_at": self._state["updated_at"],
        }

    # -- internals ---------------------------------------------------------

    def _touch(self) -> None:
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._write_sidecar()

    def _write_sidecar(self) -> None:
        try:
            atomic_write_json(self.status_path, self.snapshot())
        except OSError:
            pass

    def _render(
        self,
        status: str,
        task_id: str,
        message: str,
        turn: int | None = None,
        tokens: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        color = ANSI_COLORS.get(status, ANSI_COLORS["info"])
        suffix = ""
        if turn is not None:
            suffix += f" turn={turn}"
        if tokens is not None:
            suffix += f" tokens={tokens}"
        line = f"{color}[{status}] {task_id} {message}{suffix}{ANSI_RESET}\n"
        self.stream.write(line)
        self.stream.flush()
