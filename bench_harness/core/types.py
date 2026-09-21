"""Global data models (SPEC v3, Section 3).

Defines the agent interaction trajectory, token-audit / telemetry models,
milestone results and the final evaluation report. Every model exposes
``to_dict()`` / ``from_dict()`` helpers for atomic JSON persistence plus a
compact ``summary_dict()`` for leaderboard and live-status rendering.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]

#: Soft per-task token fuse (SPEC Section 5): auditing only, never scoring.
TOKEN_BUDGET = 10_000_000


# --- 1. Agent interaction trajectory (fine-tuning core asset) ---


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCallRecord:
        return cls(
            call_id=str(data.get("call_id", "")),
            tool_name=str(data.get("tool_name", "")),
            arguments=dict(data.get("arguments", {})),
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arg_keys": sorted(self.arguments.keys()),
        }


@dataclass(frozen=True)
class ToolResultRecord:
    call_id: str
    tool_name: str
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolResultRecord:
        return cls(
            call_id=str(data.get("call_id", "")),
            tool_name=str(data.get("tool_name", "")),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            exit_code=int(data.get("exit_code", 0)),
            duration_ms=float(data.get("duration_ms", 0.0)),
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_preview": self.stdout[:200],
        }


@dataclass
class TrajectoryTurn:
    turn_index: int
    role: str
    content: str = ""
    thought: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "role": self.role,
            "content": self.content,
            "thought": self.thought,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "tool_results": [r.to_dict() for r in self.tool_results],
            "tokens": dict(self.tokens),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryTurn:
        return cls(
            turn_index=int(data.get("turn_index", 0)),
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            thought=str(data.get("thought", "")),
            tool_calls=[ToolCallRecord.from_dict(c) for c in data.get("tool_calls", [])],
            tool_results=[ToolResultRecord.from_dict(r) for r in data.get("tool_results", [])],
            tokens={k: int(v) for k, v in dict(data.get("tokens", {})).items()},
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "role": self.role,
            "n_tool_calls": len(self.tool_calls),
            "n_tool_results": len(self.tool_results),
            "tokens": dict(self.tokens),
            "content_preview": self.content[:200],
        }


@dataclass
class AgentTrajectory:
    session_id: str
    task_id: str
    model_id: str
    turns: list[TrajectoryTurn] = field(default_factory=list)
    total_tokens: int = 0
    wall_time_seconds: float = 0.0

    def add_turn(self, turn: TrajectoryTurn) -> None:
        self.turns.append(turn)
        self.recalculate_totals()

    def recalculate_totals(self) -> int:
        total = 0
        for turn in self.turns:
            total += sum(int(v) for v in turn.tokens.values())
        self.total_tokens = total
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "model_id": self.model_id,
            "turns": [t.to_dict() for t in self.turns],
            "total_tokens": self.total_tokens,
            "wall_time_seconds": self.wall_time_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTrajectory:
        return cls(
            session_id=str(data.get("session_id", "")),
            task_id=str(data.get("task_id", "")),
            model_id=str(data.get("model_id", "")),
            turns=[TrajectoryTurn.from_dict(t) for t in data.get("turns", [])],
            total_tokens=int(data.get("total_tokens", 0)),
            wall_time_seconds=float(data.get("wall_time_seconds", 0.0)),
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "model_id": self.model_id,
            "n_turns": len(self.turns),
            "total_tokens": self.total_tokens,
            "wall_time_seconds": self.wall_time_seconds,
        }


# --- 2. Scoring, token audit and telemetry models ---


@dataclass(frozen=True)
class TokenAuditMetrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    tokens_per_passed_milestone: float = 0.0
    budget_exceeded: bool = False

    @classmethod
    def from_usage(
        cls,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning_tokens: int = 0,
        passed_milestones: int = 0,
        budget: int = TOKEN_BUDGET,
    ) -> TokenAuditMetrics:
        # 主流 API 规范中 completion_tokens 已包含 reasoning_tokens，不重复相加
        total = prompt_tokens + completion_tokens
        per_milestone = (total / passed_milestones) if passed_milestones > 0 else 0.0
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total,
            tokens_per_passed_milestone=per_milestone,
            budget_exceeded=total >= budget,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenAuditMetrics:
        return cls(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            reasoning_tokens=int(data.get("reasoning_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            tokens_per_passed_milestone=float(data.get("tokens_per_passed_milestone", 0.0)),
            budget_exceeded=bool(data.get("budget_exceeded", False)),
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "tokens_per_passed_milestone": self.tokens_per_passed_milestone,
            "budget_exceeded": self.budget_exceeded,
        }


@dataclass(frozen=True)
class TelemetryMetrics:
    wall_time_seconds: float = 0.0
    reasoning_time_seconds: float = 0.0
    network_retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TelemetryMetrics:
        return cls(
            wall_time_seconds=float(data.get("wall_time_seconds", 0.0)),
            reasoning_time_seconds=float(data.get("reasoning_time_seconds", 0.0)),
            network_retry_count=int(data.get("network_retry_count", 0)),
        )

    def summary_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MilestoneResult:
    milestone_id: str
    name: str
    passed: bool
    score: float
    failure_reason: str | None = None
    diagnostics: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MilestoneResult:
        return cls(
            milestone_id=str(data.get("milestone_id", "")),
            name=str(data.get("name", "")),
            passed=bool(data.get("passed", False)),
            score=float(data.get("score", 0.0)),
            failure_reason=data.get("failure_reason"),
            diagnostics=str(data.get("diagnostics", "")),
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "passed": self.passed,
            "score": self.score,
        }


@dataclass
class EvaluationReport:
    task_id: str
    model_id: str
    timestamp: str
    passed: bool
    final_reward: float
    milestones: list[MilestoneResult] = field(default_factory=list)
    token_metrics: TokenAuditMetrics = field(default_factory=TokenAuditMetrics)
    telemetry: TelemetryMetrics = field(default_factory=TelemetryMetrics)
    ast_diff_penalty: float = 1.0
    peak_memory_bytes: int = 0
    safety_refusal: bool = False
    condition: str = "a"

    @property
    def passed_milestones(self) -> int:
        return sum(1 for m in self.milestones if m.passed)

    @property
    def milestone_score_avg(self) -> float:
        if not self.milestones:
            return 0.0
        return sum(m.score for m in self.milestones) / len(self.milestones)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "model_id": self.model_id,
            "timestamp": self.timestamp,
            "passed": self.passed,
            "final_reward": self.final_reward,
            "milestones": [m.to_dict() for m in self.milestones],
            "token_metrics": self.token_metrics.to_dict(),
            "telemetry": self.telemetry.to_dict(),
            "ast_diff_penalty": self.ast_diff_penalty,
            "peak_memory_bytes": self.peak_memory_bytes,
            "safety_refusal": self.safety_refusal,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationReport:
        token_metrics = data.get("token_metrics", {})
        telemetry = data.get("telemetry", {})
        return cls(
            task_id=str(data.get("task_id", "")),
            model_id=str(data.get("model_id", "")),
            timestamp=str(data.get("timestamp", "")),
            passed=bool(data.get("passed", False)),
            final_reward=float(data.get("final_reward", 0.0)),
            milestones=[MilestoneResult.from_dict(m) for m in data.get("milestones", [])],
            token_metrics=(
                TokenAuditMetrics.from_dict(token_metrics) if isinstance(token_metrics, dict) else TokenAuditMetrics()
            ),
            telemetry=(TelemetryMetrics.from_dict(telemetry) if isinstance(telemetry, dict) else TelemetryMetrics()),
            ast_diff_penalty=float(data.get("ast_diff_penalty", 1.0)),
            peak_memory_bytes=int(data.get("peak_memory_bytes", 0)),
            safety_refusal=bool(data.get("safety_refusal", False)),
            condition="b" if data.get("condition") == "b" else "a",
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "model_id": self.model_id,
            "passed": self.passed,
            "final_reward": self.final_reward,
            "milestones_passed": f"{self.passed_milestones}/{len(self.milestones)}",
            "total_tokens": self.token_metrics.total_tokens,
            "ast_diff_penalty": self.ast_diff_penalty,
            "safety_refusal": self.safety_refusal,
            "condition": self.condition,
        }
