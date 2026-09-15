"""Atomic evaluation persistence, leaderboard ranking and Pareto frontier."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.snapshot import atomic_write_json
from benchmark_v3.bench_harness.core.types import EvaluationReport

EVALUATION_FILENAME = "evaluation.json"
SUMMARY_FILENAME = "summary.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReportManager:
    """Persist ``evaluation.json`` / ``summary.json`` atomically and rank runs."""

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence -------------------------------------------------------

    def save_evaluation(self, report: EvaluationReport) -> Path:
        """Atomically write ``evaluation.json`` for a single task run."""
        return atomic_write_json(self.out_dir / EVALUATION_FILENAME, report.to_dict())

    def load_evaluation(self, path: str | Path | None = None) -> EvaluationReport:
        import json

        target = Path(path) if path else self.out_dir / EVALUATION_FILENAME
        return EvaluationReport.from_dict(json.loads(target.read_text(encoding="utf-8")))

    def save_summary(self, summary: dict[str, Any]) -> Path:
        """Atomically write the aggregated ``summary.json`` leaderboard."""
        return atomic_write_json(self.out_dir / SUMMARY_FILENAME, summary)

    # -- aggregation ---------------------------------------------------------

    @staticmethod
    def token_efficiency(report: EvaluationReport) -> float:
        """Reward per token (tie-breaker / cost-effectiveness reference)."""
        total = report.token_metrics.total_tokens
        return report.final_reward / max(total, 1)

    @classmethod
    def build_summary(cls, reports: list[EvaluationReport]) -> dict[str, Any]:
        ranked = cls.rank(reports)
        n_passed = sum(1 for r in reports if r.passed)
        return {
            "generated_at": _utc_now_iso(),
            "n_reports": len(reports),
            "n_passed": n_passed,
            "pass_rate": (n_passed / len(reports)) if reports else 0.0,
            "entries": ranked,
        }

    @classmethod
    def rank(cls, reports: list[EvaluationReport]) -> list[dict[str, Any]]:
        """Rank by ``final_reward`` desc, token efficiency desc as tie-breaker.

        Each entry carries ``rank`` (1-based), ``pareto`` membership and the
        compact report summary.
        """
        pareto = cls.pareto_frontier(reports)
        order = sorted(
            range(len(reports)),
            key=lambda i: (reports[i].final_reward, cls.token_efficiency(reports[i])),
            reverse=True,
        )
        ranked: list[dict[str, Any]] = []
        for position, idx in enumerate(order, start=1):
            entry = reports[idx].summary_dict()
            entry["rank"] = position
            entry["pareto"] = bool(pareto[idx])
            entry["token_efficiency"] = cls.token_efficiency(reports[idx])
            ranked.append(entry)
        return ranked

    @staticmethod
    def pareto_frontier(reports: list[EvaluationReport]) -> list[bool]:
        """Mark non-dominated runs (max reward, min total tokens).

        A run is dominated when another run has ``reward >=`` and
        ``tokens <=`` with at least one strict inequality.
        """
        flags = [True] * len(reports)
        for i, candidate in enumerate(reports):
            for j, other in enumerate(reports):
                if i == j:
                    continue
                if (
                    other.final_reward >= candidate.final_reward
                    and other.token_metrics.total_tokens <= candidate.token_metrics.total_tokens
                    and (
                        other.final_reward > candidate.final_reward
                        or other.token_metrics.total_tokens < candidate.token_metrics.total_tokens
                    )
                ):
                    flags[i] = False
                    break
        return flags
