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


LEADERBOARD_JSON_PATH = Path("bench_runs/leaderboard.json")
LEADERBOARD_MD_PATH = Path("LEADERBOARD.md")


class MasterLeaderboard:
    """Persistent cross-model master leaderboard manager.

    Maintains `bench_runs/leaderboard.json` and automatically updates
    the human-readable `LEADERBOARD.md` in the repository root.
    """

    @classmethod
    def load_data(cls) -> dict[str, Any]:
        import json
        if not LEADERBOARD_JSON_PATH.is_file():
            return {}
        try:
            data = json.loads(LEADERBOARD_JSON_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @classmethod
    def save_data(cls, data: dict[str, Any]) -> None:
        LEADERBOARD_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(LEADERBOARD_JSON_PATH, data)

    @classmethod
    def update_leaderboard(
        cls,
        reports: list[EvaluationReport],
        model_id: str,
        driver: str,
        effort: str | None = None,
        output_dir: Path | None = None,
        wall_time: float = 0.0,
    ) -> Path:
        """Update the master leaderboard with the latest completed run."""
        if not reports:
            return LEADERBOARD_MD_PATH

        all_milestones = [m for r in reports for m in r.milestones]
        total_ms = len(all_milestones)
        passed_ms = sum(1 for m in all_milestones if m.passed)
        ms_pct = (passed_ms / total_ms * 100.0) if total_ms else 0.0

        # 归一化综合能力得分 (0~100)
        norm_scores = [
            r.final_reward if r.task_id == "audit_bundle" else r.final_reward * 100.0
            for r in reports
        ]
        capability_idx = (sum(norm_scores) / len(norm_scores)) if norm_scores else 0.0

        # 分维度提取均分
        critic_r = next((r for r in reports if r.task_id == "audit_bundle"), None)
        critic_score = f"{critic_r.final_reward:.1f}" if critic_r else "-"

        rev_rs = [r for r in reports if r.task_id in ("lock_ordering", "api_drift", "bait_guard")]
        rev_score = f"{(sum(r.final_reward for r in rev_rs) / len(rev_rs)):.2f}" if rev_rs else "-"

        short_rs = [r for r in reports if r.task_id in ("varint_parser", "timing_wheel", "lexer_state_machine")]
        short_score = f"{(sum(r.final_reward for r in short_rs) / len(short_rs)):.2f}" if short_rs else "-"

        long_rs = [r for r in reports if r.task_id in ("raft_cluster", "saga_coordinator")]
        long_score = f"{(sum(r.final_reward for r in long_rs) / len(long_rs)):.2f}" if long_rs else "-"

        total_tokens = sum(r.token_metrics.total_tokens for r in reports)

        entry_key = f"{model_id}@{effort or 'default'}"
        entry = {
            "model_id": model_id,
            "driver": driver,
            "effort": effort or "default",
            "capability_index": round(capability_idx, 1),
            "scoring_points_passed": passed_ms,
            "scoring_points_total": total_ms,
            "scoring_points_pct": round(ms_pct, 1),
            "critic_score": critic_score,
            "reviewer_score": rev_score,
            "short_score": short_score,
            "long_score": long_score,
            "total_tokens": total_tokens,
            "wall_time_seconds": round(wall_time, 1),
            "run_dir": str(output_dir) if output_dir else "",
            "updated_at": _utc_now_iso(),
        }

        data = cls.load_data()
        data[entry_key] = entry
        cls.save_data(data)

        # 重新生成 LEADERBOARD.md
        cls.export_markdown(data)
        return LEADERBOARD_MD_PATH

    @classmethod
    def export_markdown(cls, data: dict[str, Any] | None = None) -> str:
        data = data if data is not None else cls.load_data()
        entries = list(data.values())

        # 排序规则：综合能力指数降序 -> 评分点通过率降序 -> Token 消耗升序 (帕累托高效优先)
        sorted_entries = sorted(
            entries,
            key=lambda x: (
                x.get("capability_index", 0.0),
                x.get("scoring_points_pct", 0.0),
                -x.get("total_tokens", 0),
            ),
            reverse=True,
        )

        medals = ["👑 1", "🥈 2", "🥉 3"]
        now_str = _utc_now_iso()

        lines = [
            "# 🏆 Benchmark v3 全维度权威榜单 (Master Leaderboard)",
            "",
            f"> **最新更新**: `{now_str}`  ",
            "> **全量评测维度**: 次世代短任务(30点) · 次世代长任务(20点) · Reviewer调试(12点) · Critic盲审(4点) = **共 66 细粒度评分点**  ",
            "> **排序规则**: 综合能力指数降序 ➔ 全量评分点通过率降序 ➔ Token 消耗升序 (性价比帕累托优先)",
            "",
            "| 排名 | 模型标识 (Model ID) | 驱动 / 思考强度 (Driver / Effort) | 综合能力指数 | 全量评分点通过率 (66点) | Critic 盲审 | Reviewer 调试 | 短任务微引擎 | 长任务混沌 | 总 Token 消耗 | 耗时 | 制品追溯 |",
            "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | ---: | ---: | :---: |",
        ]

        for i, item in enumerate(sorted_entries):
            rank_str = medals[i] if i < len(medals) else str(i + 1)
            m_id = item.get("model_id", "unknown")
            drv = item.get("driver", "openai")
            eff = item.get("effort", "default")
            cap = item.get("capability_index", 0.0)
            pts_p = item.get("scoring_points_passed", 0)
            pts_t = item.get("scoring_points_total", 66)
            pts_pct = item.get("scoring_points_pct", 0.0)
            c_sc = item.get("critic_score", "-")
            rev_sc = item.get("reviewer_score", "-")
            sh_sc = item.get("short_score", "-")
            lg_sc = item.get("long_score", "-")
            tokens = item.get("total_tokens", 0)
            wt = item.get("wall_time_seconds", 0.0)
            r_dir = item.get("run_dir", "")
            link = f"[查看日志]({r_dir})" if r_dir else "-"

            lines.append(
                f"| {rank_str} | **`{m_id}`** | `{drv}` · `{eff}` | **`{cap:.1f} / 100`** | **`{pts_p}/{pts_t}`** (`{pts_pct:.1f}%`) | `{c_sc}` | `{rev_sc}` | `{sh_sc}` | `{lg_sc}` | `{tokens:,}` | `{wt:.1f}s` | {link} |"
            )

        lines.append("")
        lines.append("---")
        lines.append("*由 Benchmark v3 自动化轻量 Harness 驱动，每次评测完成自动增量对齐落盘。*")
        lines.append("")

        md_content = "\n".join(lines)
        try:
            LEADERBOARD_MD_PATH.write_text(md_content, encoding="utf-8")
        except OSError:
            pass
        return md_content
