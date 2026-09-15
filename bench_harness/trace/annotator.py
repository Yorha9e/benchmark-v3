"""Milestone/reward annotation and failure attribution for trajectories.

:class:`TraceAnnotator` binds an :class:`EvaluationReport` (milestone test
results + final reward) to an :class:`AgentTrajectory`, producing an
:class:`AnnotatedTrajectory` with:

* ``turn_rewards`` — turn-level credit assignment.  Passed runs split the
  (0..1-normalized) final reward uniformly over scored (assistant/tool)
  turns; failed runs distribute the mean milestone score as partial credit
  with the attributed step zeroed out.
* ``reward_tags`` — one tag per turn: ``context`` (user), ``credit``
  (passed scored turn), ``partial`` (failed scored turn), ``error`` (turn
  whose tool output failed), ``blame`` (failure attribution step).
* ``failure_attribution_step`` — the first turn where execution deviated,
  hallucinated, or introduced a fatal defect, found by a documented
  deterministic hierarchy (tool exit-code → error signals → milestone
  diagnostics overlap → earliest assistant decision fallback).
* ``milestone_binding`` — each milestone bound to a turn index
  (failed milestones bind to the attribution step, passed ones to ``None``).

Only the Python standard library is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ._compat import (
    AgentTrajectory,
    EvaluationReport,
    TrajectoryTurn,
    trajectory_to_dict,
)

__all__ = ["AnnotatedTrajectory", "TraceAnnotator"]

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}")


def _normalize_reward(reward: float) -> float:
    """Map 0..1 and 0..100 (percent) reward scales onto 0..1."""
    try:
        value = float(reward)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _turn_text(turn: TrajectoryTurn) -> str:
    chunks = [turn.content or "", turn.thought or ""]
    for result in turn.tool_results or []:
        chunks.extend([result.stdout or "", result.stderr or ""])
    return "\n".join(c for c in chunks if c)


@dataclass
class AnnotatedTrajectory:
    """A trajectory bound to its evaluation outcome."""

    trajectory: AgentTrajectory
    passed: bool = False
    final_reward: float = 0.0
    turn_rewards: list[float] = field(default_factory=list)
    reward_tags: list[str] = field(default_factory=list)
    milestone_binding: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure_attribution_step: int | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trajectory": trajectory_to_dict(self.trajectory),
            "passed": self.passed,
            "final_reward": self.final_reward,
            "turn_rewards": list(self.turn_rewards),
            "reward_tags": list(self.reward_tags),
            "milestone_binding": {
                k: dict(v) for k, v in self.milestone_binding.items()
            },
            "failure_attribution_step": self.failure_attribution_step,
            "failure_reason": self.failure_reason,
        }


class TraceAnnotator:
    """Binds milestone results and rewards to trajectory turns."""

    #: Case-insensitive signals of execution failure in tool output/content.
    ERROR_KEYWORDS = (
        "error",
        "exception",
        "traceback",
        "failed",
        "failure",
        "timeout",
        "timed out",
        "killed",
        "sigkill",
        "refused",
        "denied",
        "not found",
        "enoent",
        "enospc",
        "segfault",
        "panic",
    )

    def annotate(
        self,
        trajectory: AgentTrajectory,
        evaluation_report: EvaluationReport,
    ) -> AnnotatedTrajectory:
        """Annotate ``trajectory`` with ``evaluation_report``.

        Never raises on ragged input: empty trajectories yield zero rewards
        and ``None`` attribution for passed reports.
        """
        turns = list(trajectory.turns or [])
        passed = bool(getattr(evaluation_report, "passed", False))
        final_reward = float(getattr(evaluation_report, "final_reward", 0.0))
        norm_reward = _normalize_reward(final_reward)

        step, reason = self.find_failure_step(trajectory, evaluation_report)

        scored = [
            i for i, t in enumerate(turns) if t.role in ("assistant", "tool")
        ]
        turn_rewards = [0.0] * len(turns)
        reward_tags = ["context"] * len(turns)

        if passed:
            share = norm_reward / len(scored) if scored else 0.0
            for i in scored:
                turn_rewards[i] = share
                reward_tags[i] = "credit"
        else:
            milestones = list(getattr(evaluation_report, "milestones", []) or [])
            if milestones:
                partial = sum(
                    max(0.0, min(1.0, float(m.score or 0.0)))
                    for m in milestones
                ) / len(milestones)
            else:
                partial = 0.0
            share = partial / len(scored) if scored else 0.0
            for i in scored:
                turn_rewards[i] = share
                reward_tags[i] = "partial"
            if step is not None and 0 <= step < len(turns):
                turn_rewards[step] = 0.0
                reward_tags[step] = "blame"
        for i, turn in enumerate(turns):
            if any(
                (r.exit_code or 0) != 0 for r in (turn.tool_results or [])
            ):
                if reward_tags[i] not in ("blame",):
                    reward_tags[i] = "error" if not passed else reward_tags[i]

        binding: dict[str, dict[str, Any]] = {}
        for milestone in (
            getattr(evaluation_report, "milestones", []) or []
        ):
            binding[str(milestone.milestone_id)] = {
                "name": milestone.name,
                "passed": bool(milestone.passed),
                "score": float(milestone.score or 0.0),
                "failure_reason": milestone.failure_reason,
                # Failed milestones bind to the attributed step; passed
                # milestones carry no localized turn (None).
                "bound_turn": (
                    step if not milestone.passed else None
                ),
            }

        return AnnotatedTrajectory(
            trajectory=trajectory,
            passed=passed,
            final_reward=final_reward,
            turn_rewards=turn_rewards,
            reward_tags=reward_tags,
            milestone_binding=binding,
            failure_attribution_step=step,
            failure_reason=reason,
        )

    # -- failure attribution -----------------------------------------------

    def find_failure_step(
        self,
        trajectory: AgentTrajectory,
        evaluation_report: EvaluationReport,
    ) -> tuple[int | None, str | None]:
        """Locate the first deviating turn.

        Hierarchy (first hit wins):

        1. ``tool_error`` — first turn holding a tool result with nonzero
           ``exit_code``.
        2. ``error_signal`` — first turn whose content/thought/tool output
           contains an :data:`ERROR_KEYWORDS` signal.
        3. ``milestone_match`` — first turn with the largest token overlap
           against failed milestone ``failure_reason``/``diagnostics`` text.
        4. ``earliest_decision`` — fallback to the first assistant turn when
           the report failed but no localized signal exists.
        5. ``(None, None)`` — the report passed (nothing to attribute), or
           the trajectory has no assistant turn at all.
        """
        if bool(getattr(evaluation_report, "passed", False)):
            return None, None
        turns = list(trajectory.turns or [])

        for turn in turns:
            for result in turn.tool_results or []:
                if (result.exit_code or 0) != 0:
                    return turn.turn_index, (
                        f"tool_error: {result.tool_name} "
                        f"({result.call_id}) exited "
                        f"{result.exit_code}"
                    )

        lowered = [_turn_text(t).lower() for t in turns]
        for turn, text in zip(turns, lowered):
            if any(keyword in text for keyword in self.ERROR_KEYWORDS):
                return turn.turn_index, (
                    f"error_signal in {turn.role} turn {turn.turn_index}"
                )

        failed_texts = []
        for milestone in (
            getattr(evaluation_report, "milestones", []) or []
        ):
            if not milestone.passed:
                failed_texts.append(
                    " ".join(
                        s
                        for s in (
                            str(milestone.failure_reason or ""),
                            str(milestone.diagnostics or ""),
                            str(milestone.name or ""),
                        )
                        if s
                    ).lower()
                )
        if failed_texts:
            best_idx: int | None = None
            best_overlap = 0
            for turn, text in zip(turns, lowered):
                tokens = set(_TOKEN_RE.findall(text))
                if not tokens:
                    continue
                overlap = 0
                for failed in failed_texts:
                    failed_tokens = set(_TOKEN_RE.findall(failed))
                    overlap = max(overlap, len(tokens & failed_tokens))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_idx = turn.turn_index
            if best_idx is not None and best_overlap > 0:
                return best_idx, (
                    "milestone_match: turn content overlaps failed "
                    "milestone diagnostics"
                )

        for turn in turns:
            if turn.role == "assistant":
                return turn.turn_index, (
                    "earliest_decision: no localized defect signal; "
                    "attributed to first assistant turn"
                )
        return None, None
