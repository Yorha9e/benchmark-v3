"""Streaming trajectory ingestion for agent sessions.

:class:`TraceCollector` is the harness-side "session cable": the agent
tool-use loop calls :meth:`TraceCollector.start_session` once and then
records every user turn, assistant turn (content + chain-of-thought +
tool calls + token usage) and tool execution result.  Two artefacts are
produced simultaneously:

1. ``wire.jsonl`` — an append-only, line-delimited JSON event log streamed
   live to disk, so a crashed session can be replayed without losing the
   prefix (see :meth:`TraceCollector.load_wire`).
2. :class:`AgentTrajectory` — the full in-memory trajectory object defined
   in the harness spec §3 (``bench_harness.core.types``).

Only the Python standard library is used.  All disk writes are single-line
appends followed by a flush, so a torn write can corrupt at most the last
line (which :meth:`load_wire` skips defensively).
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any

from ._compat import (
    AgentTrajectory,
    ToolCallRecord,
    ToolResultRecord,
    TrajectoryTurn,
    trajectory_from_dict,
    trajectory_to_dict,
)

__all__ = ["TraceCollector", "WIRE_VERSION"]

WIRE_VERSION = 1


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _coerce_tool_call(item: Any, auto_id: str) -> ToolCallRecord:
    """Normalize a user-supplied tool call into a :class:`ToolCallRecord`.

    Accepts a :class:`ToolCallRecord` (passed through) or a plain dict with
    the tolerant key aliases ``id``/``call_id``, ``name``/``tool_name``/
    ``function`` and ``arguments``/``input``/``args`` (str arguments are
    JSON-decoded when possible).
    """
    if isinstance(item, ToolCallRecord):
        return item
    if isinstance(item, dict):
        call_id = (
            item.get("call_id") or item.get("id") or item.get("tool_call_id")
        )
        name = (
            item.get("tool_name")
            or item.get("name")
            or item.get("function")
            or "bash"
        )
        args = item.get("arguments", item.get("input", item.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except (ValueError, AttributeError):
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_value": args}
        return ToolCallRecord(
            call_id=str(call_id) if call_id else auto_id,
            tool_name=str(name),
            arguments=dict(args),
        )
    raise TypeError(
        f"tool_calls entries must be ToolCallRecord or dict, got {type(item)!r}"
    )


def _coerce_tokens(tokens: Any) -> dict[str, int]:
    """Normalize token usage dicts to ``{prompt, completion, reasoning}``."""
    if not tokens:
        return {}
    if not isinstance(tokens, dict):
        raise TypeError(f"tokens must be a dict, got {type(tokens)!r}")
    out: dict[str, int] = {}
    aliases = {
        "prompt": ("prompt", "prompt_tokens", "input_tokens"),
        "completion": ("completion", "completion_tokens", "output_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
    }
    for canonical, keys in aliases.items():
        for key in keys:
            if key in tokens and tokens[key] is not None:
                try:
                    out[canonical] = int(tokens[key])
                except (TypeError, ValueError):
                    out[canonical] = 0
                break
    return out


class TraceCollector:
    """Streaming ingestor for one agent session's trajectory.

    Typical harness usage::

        collector = TraceCollector(wire_path="runs/<session>/wire.jsonl")
        collector.start_session(session_id, task_id, model_id)
        collector.record_user_turn("Fix the deadlock in ...")
        collector.record_assistant_turn(content, thought, tool_calls, tokens)
        collector.record_tool_result(call_id, tool, stdout, stderr, code, ms)
        trajectory = collector.finish()
        collector.close()
    """

    def __init__(self, wire_path: str | os.PathLike[str] | None = None) -> None:
        self._wire_path = os.fspath(wire_path) if wire_path else None
        self._wire_fh = None
        self._session_id: str | None = None
        self._task_id: str | None = None
        self._model_id: str | None = None
        self._turns: list[TrajectoryTurn] = []
        self._total_tokens = 0
        self._start_monotonic = 0.0
        self._call_counter = 0
        self._finished = False

    # -- session lifecycle -------------------------------------------------

    def start_session(
        self,
        session_id: str,
        task_id: str,
        model_id: str,
        wire_path: str | os.PathLike[str] | None = None,
    ) -> None:
        """Begin a new session, resetting all buffered state.

        If ``wire_path`` is given (here or at construction) the wire log is
        opened in append mode and a ``session_start`` envelope is emitted.
        """
        self.close()
        self._session_id = str(session_id)
        self._task_id = str(task_id)
        self._model_id = str(model_id)
        self._turns = []
        self._total_tokens = 0
        self._call_counter = 0
        self._finished = False
        self._start_monotonic = time.monotonic()
        if wire_path is not None:
            self._wire_path = os.fspath(wire_path)
        if self._wire_path:
            parent = os.path.dirname(os.path.abspath(self._wire_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._wire_fh = open(
                self._wire_path, "a", encoding="utf-8", buffering=1
            )
        self._emit(
            "session_start",
            {
                "task_id": self._task_id,
                "model_id": self._model_id,
                "wire_version": WIRE_VERSION,
            },
        )

    def _require_session(self) -> None:
        if self._session_id is None:
            raise RuntimeError(
                "TraceCollector.start_session() must be called before "
                "recording turns."
            )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    # -- wire streaming ----------------------------------------------------

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._wire_fh is None:
            return
        envelope = {
            "v": WIRE_VERSION,
            "event": event,
            "session_id": self._session_id,
            "ts": _utc_now(),
            "data": data,
        }
        line = json.dumps(envelope, ensure_ascii=False, default=str)
        self._wire_fh.write(line + "\n")
        self._wire_fh.flush()

    # -- ingestion ----------------------------------------------------------

    def record_user_turn(self, content: str) -> TrajectoryTurn:
        """Record a ``user`` turn and return it."""
        self._require_session()
        turn = TrajectoryTurn(
            turn_index=len(self._turns),
            role="user",
            content=str(content or ""),
        )
        self._turns.append(turn)
        self._emit(
            "user_turn",
            {"turn_index": turn.turn_index, "content": turn.content},
        )
        return turn

    def record_assistant_turn(
        self,
        content: str = "",
        thought: str = "",
        tool_calls: list[Any] | None = None,
        tokens: dict[str, Any] | None = None,
    ) -> TrajectoryTurn:
        """Record an ``assistant`` turn and return it.

        ``tool_calls`` accepts :class:`ToolCallRecord` items or plain dicts
        (see :func:`_coerce_tool_call`).  ``tokens`` accepts
        ``{prompt, completion, reasoning}`` or the ``*_tokens`` /
        ``input_tokens``/``output_tokens`` aliases.  The trajectory's
        ``total_tokens`` grows by ``prompt + completion + reasoning``.
        """
        self._require_session()
        coerced_calls: list[ToolCallRecord] = []
        for item in tool_calls or []:
            self._call_counter += 1
            coerced_calls.append(
                _coerce_tool_call(item, f"call_{self._call_counter:04d}")
            )
        token_dict = _coerce_tokens(tokens)
        self._total_tokens += (
            token_dict.get("prompt", 0)
            + token_dict.get("completion", 0)
            + token_dict.get("reasoning", 0)
        )
        turn = TrajectoryTurn(
            turn_index=len(self._turns),
            role="assistant",
            content=str(content or ""),
            thought=str(thought or ""),
            tool_calls=coerced_calls,
            tokens=token_dict,
        )
        self._turns.append(turn)
        self._emit(
            "assistant_turn",
            {
                "turn_index": turn.turn_index,
                "content": turn.content,
                "thought": turn.thought,
                "tool_calls": [
                    {
                        "call_id": c.call_id,
                        "tool_name": c.tool_name,
                        "arguments": c.arguments,
                    }
                    for c in coerced_calls
                ],
                "tokens": token_dict,
            },
        )
        return turn

    def record_tool_result(
        self,
        call_id: str,
        tool_name: str,
        stdout: str,
        stderr: str = "",
        exit_code: int = 0,
        duration_ms: float = 0.0,
    ) -> ToolResultRecord:
        """Record a tool execution result and return it.

        The result is attached to the most recent ``assistant`` turn that
        issued ``call_id``.  If no turn issued it (orphan result, e.g. a
        background probe), a standalone ``role="tool"`` turn is created so
        no output is ever silently dropped.
        """
        self._require_session()
        record = ToolResultRecord(
            call_id=str(call_id),
            tool_name=str(tool_name),
            stdout=str(stdout or ""),
            stderr=str(stderr or ""),
            exit_code=int(exit_code),
            duration_ms=float(duration_ms),
        )
        host: TrajectoryTurn | None = None
        for turn in reversed(self._turns):
            if turn.role != "assistant":
                continue
            if any(c.call_id == record.call_id for c in turn.tool_calls):
                host = turn
                break
        if host is None:
            host = TrajectoryTurn(
                turn_index=len(self._turns),
                role="tool",
                tool_results=[record],
            )
            self._turns.append(host)
        else:
            host.tool_results.append(record)
        self._emit(
            "tool_result",
            {
                "turn_index": host.turn_index,
                "call_id": record.call_id,
                "tool_name": record.tool_name,
                "stdout": record.stdout,
                "stderr": record.stderr,
                "exit_code": record.exit_code,
                "duration_ms": record.duration_ms,
            },
        )
        return record

    # -- trajectory build / persistence -------------------------------------

    def get_trajectory(self) -> AgentTrajectory:
        """Return a snapshot :class:`AgentTrajectory` of turns so far."""
        self._require_session()
        return AgentTrajectory(
            session_id=self._session_id or "",
            task_id=self._task_id or "",
            model_id=self._model_id or "",
            turns=copy.deepcopy(self._turns),
            total_tokens=self._total_tokens,
            wall_time_seconds=max(
                0.0, time.monotonic() - self._start_monotonic
            ),
        )

    def finish(self, wall_time_seconds: float | None = None) -> AgentTrajectory:
        """Seal the session (emits ``session_end``) and return the trajectory."""
        self._require_session()
        trajectory = self.get_trajectory()
        if wall_time_seconds is not None:
            trajectory.wall_time_seconds = max(0.0, float(wall_time_seconds))
        self._finished = True
        self._emit(
            "session_end",
            {
                "turns": len(self._turns),
                "total_tokens": self._total_tokens,
                "wall_time_seconds": trajectory.wall_time_seconds,
            },
        )
        return trajectory

    def save_trajectory(self, output_path: str | os.PathLike[str]) -> str:
        """Atomically persist the current trajectory as JSON."""
        self._require_session()
        path = os.fspath(output_path)
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = trajectory_to_dict(self.get_trajectory())
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
        return path

    @staticmethod
    def load_trajectory(path: str | os.PathLike[str]) -> AgentTrajectory:
        """Load a trajectory written by :meth:`save_trajectory`."""
        with open(os.fspath(path), encoding="utf-8") as fh:
            return trajectory_from_dict(json.load(fh))

    @classmethod
    def load_wire(
        cls, wire_path: str | os.PathLike[str]
    ) -> AgentTrajectory:
        """Replay a ``wire.jsonl`` event log into an :class:`AgentTrajectory`.

        Torn trailing lines (from a killed session) are skipped.  Token
        totals are recomputed with the same rule as live ingestion.
        """
        replay = cls(wire_path=None)
        with open(os.fspath(wire_path), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except ValueError:
                    continue  # torn line from a crash; skip
                event = envelope.get("event")
                data = envelope.get("data", {}) or {}
                if event == "session_start":
                    replay._session_id = envelope.get("session_id", "")
                    replay._task_id = str(data.get("task_id", ""))
                    replay._model_id = str(data.get("model_id", ""))
                    replay._start_monotonic = time.monotonic()
                elif event == "user_turn" and replay._session_id is not None:
                    replay.record_user_turn(str(data.get("content", "")))
                elif (
                    event == "assistant_turn"
                    and replay._session_id is not None
                ):
                    replay.record_assistant_turn(
                        content=str(data.get("content", "")),
                        thought=str(data.get("thought", "")),
                        tool_calls=data.get("tool_calls", []) or [],
                        tokens=data.get("tokens", {}) or {},
                    )
                elif (
                    event == "tool_result" and replay._session_id is not None
                ):
                    replay.record_tool_result(
                        call_id=str(data.get("call_id", "")),
                        tool_name=str(data.get("tool_name", "")),
                        stdout=str(data.get("stdout", "")),
                        stderr=str(data.get("stderr", "")),
                        exit_code=int(data.get("exit_code", 0) or 0),
                        duration_ms=float(data.get("duration_ms", 0.0) or 0.0),
                    )
        if replay._session_id is None:
            raise ValueError(f"no session_start event found in {wire_path}")
        return replay.get_trajectory()

    # -- resource handling ---------------------------------------------------

    def close(self) -> None:
        """Flush and close the wire file handle, if open."""
        if self._wire_fh is not None:
            try:
                self._wire_fh.flush()
                self._wire_fh.close()
            finally:
                self._wire_fh = None

    def __enter__(self) -> TraceCollector:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session_id is not None and not self._finished:
            try:
                self.finish()
            except Exception:
                pass
        self.close()
