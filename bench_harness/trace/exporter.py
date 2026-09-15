"""Training-dataset exporters for annotated trajectories.

:class:`DatasetExporter` produces the two fine-tuning artefacts of the
harness spec §2/§5:

* :meth:`export_sft_golden` — clean Pass@1 golden trajectories as JSONL for
  SFT training.  Low-reward runs, information-poor runs (no assistant
  content) and bloated runs (tokens per passed milestone above
  ``max_tokens_per_milestone``) are filtered out, per the spec's
  high-signal-density rule.
* :meth:`export_dpo_pairs` — RL/DPO preference pairs
  ``(prompt, chosen, rejected, failure_attribution)`` as JSONL.  Accepts
  explicit ``(chosen, rejected)`` pairs (tuples, lists or
  ``{"chosen", "rejected"}`` dicts) or a flat list of annotated
  trajectories that is auto-paired per ``task_id`` (best vs. worst with a
  strictly positive reward gap).

Chosen/rejected items may be :class:`AnnotatedTrajectory`,
:class:`AgentTrajectory`, or already-normalized message lists.  Messages are
stored in the OpenAI tools format (see :class:`TrajectoryNormalizer`); the
``prompt`` is the common message prefix shared by both sides.

All writes are atomic (temporary file + :func:`os.replace`) and use only
the Python standard library.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._compat import AgentTrajectory
from .annotator import AnnotatedTrajectory
from .normalizer import TrajectoryNormalizer

__all__ = ["DatasetExporter"]


def _normalize_reward(reward: Any) -> float:
    try:
        value = float(reward)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:  # percent scale
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _as_trajectory(item: Any) -> AgentTrajectory | None:
    if isinstance(item, AnnotatedTrajectory):
        return item.trajectory
    if isinstance(item, AgentTrajectory):
        return item
    return None


def _as_messages(item: Any) -> list[dict[str, Any]] | None:
    trajectory = _as_trajectory(item)
    if trajectory is not None:
        return TrajectoryNormalizer.to_openai_tools(trajectory)
    if isinstance(item, list) and all(isinstance(m, dict) for m in item):
        return [dict(m) for m in item]
    return None


def _annotated_meta(item: Any) -> dict[str, Any]:
    if isinstance(item, AnnotatedTrajectory):
        return {
            "reward": float(item.final_reward),
            "passed": bool(item.passed),
            "failure_attribution_step": item.failure_attribution_step,
            "failure_reason": item.failure_reason,
        }
    trajectory = _as_trajectory(item)
    if trajectory is not None:
        return {
            "reward": 0.0,
            "passed": False,
            "failure_attribution_step": None,
            "failure_reason": None,
        }
    if isinstance(item, dict):
        return {
            "reward": float(item.get("reward", 0.0) or 0.0),
            "passed": bool(item.get("passed", False)),
            "failure_attribution_step": item.get("failure_attribution_step"),
            "failure_reason": item.get("failure_reason"),
        }
    return {
        "reward": 0.0,
        "passed": False,
        "failure_attribution_step": None,
        "failure_reason": None,
    }


def _common_prefix(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    for a, b in zip(first, second):
        if a == b:
            prefix.append(a)
        else:
            break
    return prefix


def _write_jsonl_atomic(
    path: str, rows: list[dict[str, Any]]
) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(
                json.dumps(row, ensure_ascii=False, default=str) + "\n"
            )
    os.replace(tmp_path, path)
    return path


class DatasetExporter:
    """Exports SFT golden sets and DPO preference pairs as JSONL."""

    # -- SFT golden ----------------------------------------------------------

    def export_sft_golden(
        self,
        annotated_trajectories: list[AnnotatedTrajectory],
        output_path: str | os.PathLike[str],
        min_reward: float = 1.0,
        max_tokens_per_milestone: float | None = None,
    ) -> dict[str, Any]:
        """Export clean Pass@1 golden trajectories for SFT training.

        Filters (in order): reward below ``min_reward`` (both compared on
        the 0..1-normalized scale), runs with no non-empty assistant turn
        (low information density), and — when ``max_tokens_per_milestone``
        is set — bloated runs whose ``total_tokens / passed_milestones``
        exceeds the cap.  Each line holds ``session_id``, ``task_id``,
        ``model_id``, ``messages`` (OpenAI tools format), ``reward``,
        ``total_tokens`` and ``turn_rewards``.
        """
        threshold = _normalize_reward(min_reward)
        rows: list[dict[str, Any]] = []
        stats = {
            "total": 0,
            "written": 0,
            "skipped_low_reward": 0,
            "skipped_low_density": 0,
            "skipped_bloated": 0,
            "output_path": os.fspath(output_path),
        }
        for annotated in annotated_trajectories or []:
            stats["total"] += 1
            trajectory = _as_trajectory(annotated)
            if trajectory is None:
                stats["skipped_low_density"] += 1
                continue
            meta = _annotated_meta(annotated)
            if _normalize_reward(meta["reward"]) < threshold:
                stats["skipped_low_reward"] += 1
                continue
            if not any(
                t.role == "assistant" and (t.content or t.tool_calls)
                for t in (trajectory.turns or [])
            ):
                stats["skipped_low_density"] += 1
                continue
            if max_tokens_per_milestone is not None:
                binding = getattr(annotated, "milestone_binding", None) or {}
                if binding:
                    passed_ms = sum(
                        1 for v in binding.values() if v.get("passed")
                    )
                    denom = max(1, passed_ms)
                else:
                    denom = max(
                        1,
                        len(
                            [
                                t
                                for t in (trajectory.turns or [])
                                if t.role in ("assistant", "tool")
                            ]
                        ),
                    )
                tokens_per_unit = trajectory.total_tokens / denom
                if tokens_per_unit > float(max_tokens_per_milestone):
                    stats["skipped_bloated"] += 1
                    continue
            rows.append(
                {
                    "session_id": trajectory.session_id,
                    "task_id": trajectory.task_id,
                    "model_id": trajectory.model_id,
                    "messages": TrajectoryNormalizer.to_openai_tools(
                        trajectory
                    ),
                    "reward": meta["reward"],
                    "total_tokens": trajectory.total_tokens,
                    "turn_rewards": list(
                        getattr(annotated, "turn_rewards", []) or []
                    ),
                }
            )
            stats["written"] += 1
        _write_jsonl_atomic(stats["output_path"], rows)
        return stats

    # -- DPO / RL preference pairs --------------------------------------------

    def export_dpo_pairs(
        self,
        pairs_or_trajectories: list[Any],
        output_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Export DPO preference pairs ``(prompt, chosen, rejected)``.

        Each output line holds ``task_id``, ``prompt`` (common message
        prefix), ``chosen`` / ``rejected`` (full OpenAI-tools message
        lists), ``failure_attribution`` (``{"step", "reason"}`` taken from
        the rejected side, falling back to the chosen side), plus
        ``chosen_reward`` / ``rejected_reward`` for audit.
        """
        pairs = self._coerce_pairs(pairs_or_trajectories or [])
        rows: list[dict[str, Any]] = []
        skipped = 0
        for chosen_item, rejected_item in pairs:
            chosen_messages = _as_messages(chosen_item)
            rejected_messages = _as_messages(rejected_item)
            if not chosen_messages or not rejected_messages:
                skipped += 1
                continue
            chosen_meta = _annotated_meta(chosen_item)
            rejected_meta = _annotated_meta(rejected_item)
            task_id = ""
            for item in (chosen_item, rejected_item):
                trajectory = _as_trajectory(item)
                if trajectory is not None and trajectory.task_id:
                    task_id = trajectory.task_id
                    break
            step = rejected_meta["failure_attribution_step"]
            reason = rejected_meta["failure_reason"]
            if step is None:
                step = chosen_meta["failure_attribution_step"]
                reason = chosen_meta["failure_reason"]
            rows.append(
                {
                    "task_id": task_id,
                    "prompt": _common_prefix(
                        chosen_messages, rejected_messages
                    ),
                    "chosen": chosen_messages,
                    "rejected": rejected_messages,
                    "failure_attribution": {"step": step, "reason": reason},
                    "chosen_reward": chosen_meta["reward"],
                    "rejected_reward": rejected_meta["reward"],
                }
            )
        out = os.fspath(output_path)
        _write_jsonl_atomic(out, rows)
        return {
            "total_pairs": len(pairs),
            "written": len(rows),
            "skipped": skipped,
            "output_path": out,
        }

    # -- pairing helpers -------------------------------------------------------

    @staticmethod
    def _coerce_pairs(items: list[Any]) -> list[tuple[Any, Any]]:
        """Normalize exporter input into ``(chosen, rejected)`` pairs."""
        pairs: list[tuple[Any, Any]] = []
        flats: list[Any] = []
        for item in items:
            if isinstance(item, dict) and (
                "chosen" in item or "rejected" in item
            ):
                pairs.append((item.get("chosen"), item.get("rejected")))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                pairs.append((item[0], item[1]))
            else:
                flats.append(item)
        pairs.extend(DatasetExporter._auto_pair(flats))
        return pairs

    @staticmethod
    def _auto_pair(items: list[Any]) -> list[tuple[Any, Any]]:
        """Pair best-vs-worst annotated trajectories sharing a ``task_id``.

        Groups by task; within a group sorts by normalized reward and pairs
        the top item (chosen) with the bottom item (rejected) only when the
        gap is strictly positive, so no tie/noise pairs leak into training.
        """
        groups: dict[str, list[Any]] = {}
        for item in items:
            trajectory = _as_trajectory(item)
            if trajectory is None:
                continue
            groups.setdefault(trajectory.task_id or "", []).append(item)
        auto: list[tuple[Any, Any]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            ranked = sorted(
                group,
                key=lambda it: _normalize_reward(
                    _annotated_meta(it)["reward"]
                ),
                reverse=True,
            )
            best_reward = _normalize_reward(
                _annotated_meta(ranked[0])["reward"]
            )
            worst_reward = _normalize_reward(
                _annotated_meta(ranked[-1])["reward"]
            )
            if best_reward > worst_reward:
                auto.append((ranked[0], ranked[-1]))
        return auto
