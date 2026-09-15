"""Shared type-compatibility shim for the trace pipeline.

The canonical data models live in ``bench_harness.core.types`` (Mission M1).
The trace pipeline must import cleanly whether or not that module has landed
yet, so this shim prefers the canonical definitions and falls back to
field-compatible local dataclasses taken verbatim from
``benchmark_v3/docs/BENCHMARK_HARNESS_SPEC_V3.md`` §3.

When the canonical module becomes available the fallback is bypassed
automatically (see :data:`CANONICAL_TYPES`), so no code change is needed at
merge time.

Only the Python standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CANONICAL_TYPES",
    "TokenAuditMetrics",
    "TelemetryMetrics",
    "ToolCallRecord",
    "ToolResultRecord",
    "TrajectoryTurn",
    "AgentTrajectory",
    "MilestoneResult",
    "EvaluationReport",
    "trajectory_to_dict",
    "trajectory_from_dict",
    "report_to_dict",
    "types_source",
]

try:  # Canonical definitions (Mission M1: bench_harness/core/types.py).
    from bench_harness.core.types import (  # type: ignore[import-not-found]
        AgentTrajectory,
        EvaluationReport,
        MilestoneResult,
        TelemetryMetrics,
        TokenAuditMetrics,
        ToolCallRecord,
        ToolResultRecord,
        TrajectoryTurn,
    )

    CANONICAL_TYPES = True
except ImportError:  # pragma: no cover - fallback path
    try:
        from benchmark_v3.bench_harness.core.types import (  # type: ignore[import-not-found]
            AgentTrajectory,
            EvaluationReport,
            MilestoneResult,
            TelemetryMetrics,
            TokenAuditMetrics,
            ToolCallRecord,
            ToolResultRecord,
            TrajectoryTurn,
        )

        CANONICAL_TYPES = True
    except ImportError:
        CANONICAL_TYPES = False

        @dataclass(frozen=True)
        class ToolCallRecord:
            call_id: str
            tool_name: str
            arguments: dict[str, Any]

        @dataclass(frozen=True)
        class ToolResultRecord:
            call_id: str
            tool_name: str
            stdout: str
            stderr: str = ""
            exit_code: int = 0
            duration_ms: float = 0.0

        @dataclass
        class TrajectoryTurn:
            turn_index: int
            role: str
            content: str = ""
            thought: str = ""
            tool_calls: list[ToolCallRecord] = field(default_factory=list)
            tool_results: list[ToolResultRecord] = field(default_factory=list)
            tokens: dict[str, int] = field(default_factory=dict)

        @dataclass
        class AgentTrajectory:
            session_id: str
            task_id: str
            model_id: str
            turns: list[TrajectoryTurn] = field(default_factory=list)
            total_tokens: int = 0
            wall_time_seconds: float = 0.0

        @dataclass(frozen=True)
        class TokenAuditMetrics:
            prompt_tokens: int = 0
            completion_tokens: int = 0
            reasoning_tokens: int = 0
            total_tokens: int = 0
            tokens_per_passed_milestone: float = 0.0
            budget_exceeded: bool = False

        @dataclass(frozen=True)
        class TelemetryMetrics:
            wall_time_seconds: float = 0.0
            reasoning_time_seconds: float = 0.0
            network_retry_count: int = 0

        @dataclass(frozen=True)
        class MilestoneResult:
            milestone_id: str
            name: str
            passed: bool
            score: float
            failure_reason: str | None = None
            diagnostics: str = ""

        @dataclass
        class EvaluationReport:
            task_id: str
            model_id: str
            timestamp: str
            passed: bool
            final_reward: float
            milestones: list[MilestoneResult] = field(default_factory=list)
            token_metrics: TokenAuditMetrics = field(
                default_factory=TokenAuditMetrics
            )
            telemetry: TelemetryMetrics = field(default_factory=TelemetryMetrics)
            ast_diff_penalty: float = 1.0
            peak_memory_bytes: int = 0
            safety_refusal: bool = False


def types_source() -> str:
    """Return where the type definitions were imported from."""
    if CANONICAL_TYPES:
        return "bench_harness.core.types"
    return "bench_harness.trace._compat (spec §3 fallback)"


def trajectory_to_dict(trajectory: AgentTrajectory) -> dict[str, Any]:
    """Serialize an :class:`AgentTrajectory` to plain JSON-compatible dict."""
    return {
        "session_id": trajectory.session_id,
        "task_id": trajectory.task_id,
        "model_id": trajectory.model_id,
        "total_tokens": trajectory.total_tokens,
        "wall_time_seconds": trajectory.wall_time_seconds,
        "turns": [
            {
                "turn_index": t.turn_index,
                "role": t.role,
                "content": t.content,
                "thought": t.thought,
                "tool_calls": [
                    {
                        "call_id": c.call_id,
                        "tool_name": c.tool_name,
                        "arguments": c.arguments,
                    }
                    for c in (t.tool_calls or [])
                ],
                "tool_results": [
                    {
                        "call_id": r.call_id,
                        "tool_name": r.tool_name,
                        "stdout": r.stdout,
                        "stderr": r.stderr,
                        "exit_code": r.exit_code,
                        "duration_ms": r.duration_ms,
                    }
                    for r in (t.tool_results or [])
                ],
                "tokens": dict(t.tokens or {}),
            }
            for t in (trajectory.turns or [])
        ],
    }


def trajectory_from_dict(data: dict[str, Any]) -> AgentTrajectory:
    """Rebuild an :class:`AgentTrajectory` from :func:`trajectory_to_dict`."""
    turns: list[TrajectoryTurn] = []
    for raw in data.get("turns", []) or []:
        turns.append(
            TrajectoryTurn(
                turn_index=int(raw.get("turn_index", len(turns))),
                role=str(raw.get("role", "user")),
                content=str(raw.get("content", "") or ""),
                thought=str(raw.get("thought", "") or ""),
                tool_calls=[
                    ToolCallRecord(
                        call_id=str(c.get("call_id", "")),
                        tool_name=str(c.get("tool_name", "")),
                        arguments=dict(c.get("arguments", {}) or {}),
                    )
                    for c in (raw.get("tool_calls", []) or [])
                ],
                tool_results=[
                    ToolResultRecord(
                        call_id=str(r.get("call_id", "")),
                        tool_name=str(r.get("tool_name", "")),
                        stdout=str(r.get("stdout", "") or ""),
                        stderr=str(r.get("stderr", "") or ""),
                        exit_code=int(r.get("exit_code", 0) or 0),
                        duration_ms=float(r.get("duration_ms", 0.0) or 0.0),
                    )
                    for r in (raw.get("tool_results", []) or [])
                ],
                tokens={
                    str(k): int(v)
                    for k, v in (raw.get("tokens", {}) or {}).items()
                },
            )
        )
    return AgentTrajectory(
        session_id=str(data.get("session_id", "")),
        task_id=str(data.get("task_id", "")),
        model_id=str(data.get("model_id", "")),
        turns=turns,
        total_tokens=int(data.get("total_tokens", 0) or 0),
        wall_time_seconds=float(data.get("wall_time_seconds", 0.0) or 0.0),
    )


def report_to_dict(report: EvaluationReport) -> dict[str, Any]:
    """Serialize an :class:`EvaluationReport` to a JSON-compatible dict."""
    tm = getattr(report, "token_metrics", None)
    tel = getattr(report, "telemetry", None)
    return {
        "task_id": report.task_id,
        "model_id": report.model_id,
        "timestamp": report.timestamp,
        "passed": report.passed,
        "final_reward": report.final_reward,
        "milestones": [
            {
                "milestone_id": m.milestone_id,
                "name": m.name,
                "passed": m.passed,
                "score": m.score,
                "failure_reason": m.failure_reason,
                "diagnostics": m.diagnostics,
            }
            for m in (report.milestones or [])
        ],
        "token_metrics": {
            "prompt_tokens": getattr(tm, "prompt_tokens", 0),
            "completion_tokens": getattr(tm, "completion_tokens", 0),
            "reasoning_tokens": getattr(tm, "reasoning_tokens", 0),
            "total_tokens": getattr(tm, "total_tokens", 0),
            "tokens_per_passed_milestone": getattr(
                tm, "tokens_per_passed_milestone", 0.0
            ),
            "budget_exceeded": getattr(tm, "budget_exceeded", False),
        },
        "telemetry": {
            "wall_time_seconds": getattr(tel, "wall_time_seconds", 0.0),
            "reasoning_time_seconds": getattr(
                tel, "reasoning_time_seconds", 0.0
            ),
            "network_retry_count": getattr(tel, "network_retry_count", 0),
        },
        "ast_diff_penalty": getattr(report, "ast_diff_penalty", 1.0),
        "peak_memory_bytes": getattr(report, "peak_memory_bytes", 0),
        "safety_refusal": getattr(report, "safety_refusal", False),
    }
