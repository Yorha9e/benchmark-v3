"""Atomic evaluation persistence, leaderboard ranking and Pareto frontier."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.snapshot import atomic_write_json, exclusive_file_lock
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

    Data model (v2): each entry keeps per-task best slots
    (``tasks: {task_id: {...}}``) so single-task reruns merge into the
    master row instead of replacing it. Aggregates (capability index =
    scoring-point pass rate over A+B slots, suite means, scoring points)
    are recomputed from the stored slots.
    Legacy entries without ``tasks`` keep their last aggregates untouched
    and are excluded from per-task drill-down boards.

    Task / suite boards are sort views of this same JSON (different
    primary keys), not independently scored tables.
    """

    #: Canonical A-condition roster (from catalog; B slots use ``task@b``).
    #: Bound lazily so ``import report`` does not cycle through ``suites``.
    CANONICAL_TASKS: tuple[str, ...] = ()
    CANONICAL_B_TASKS: tuple[str, ...] = ()
    TASK_SUITES: dict[str, str] = {}
    SUITES: tuple[str, ...] = ()
    SUITE_TASKS: dict[str, tuple[str, ...]] = {}
    SUITE_B_TASKS: dict[str, tuple[str, ...]] = {}
    SUITE_SCORE_FIELDS: dict[str, str] = {}
    SUITE_B_SCORE_FIELDS: dict[str, str] = {}

    #: Floating-point tolerance when comparing rewards (avoid churn on ties).
    REWARD_EPS = 1e-9

    #: B down-weight in the capability index. B re-tests an A-task subset
    #: under plan scaffolding, so each B milestone counts this fraction of
    #: an A milestone. Tunable; 0.2 → full board 66 + 50×0.2 = 76 points.
    B_WEIGHT = 0.2

    @classmethod
    def _bind_catalog(cls) -> None:
        if cls.CANONICAL_TASKS:
            return
        from benchmark_v3.bench_harness.suites.catalog import (
            FAMILY_KEYS,
            canonical_a_tasks,
            canonical_b_tasks,
            family_b_task_map,
            family_task_map,
            task_family_map,
        )
        cls.CANONICAL_TASKS = canonical_a_tasks()
        cls.CANONICAL_B_TASKS = canonical_b_tasks()
        cls.TASK_SUITES = task_family_map()
        cls.SUITES = FAMILY_KEYS
        cls.SUITE_TASKS = family_task_map()
        cls.SUITE_B_TASKS = family_b_task_map()
        cls.SUITE_SCORE_FIELDS = {family: f"{family}_score" for family in FAMILY_KEYS}
        cls.SUITE_B_SCORE_FIELDS = {
            family: f"{family}_b_score" for family in cls.SUITE_B_TASKS
        }

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
    def _slot_from_report(
        cls,
        report: EvaluationReport,
        driver: str,
        output_dir: Path | None,
    ) -> dict[str, Any]:
        """Collapse one task report into a storable best-slot record."""
        cls._bind_catalog()
        milestones = list(report.milestones or [])
        return {
            "task_id": report.task_id,
            "suite": cls.TASK_SUITES.get(report.task_id, "?"),
            "condition": getattr(report, "condition", "a") or "a",
            "reward": float(report.final_reward),
            "passed": bool(report.passed),
            "milestones_passed": sum(1 for m in milestones if m.passed),
            "milestones_total": len(milestones),
            "total_tokens": int(report.token_metrics.total_tokens),
            "driver": driver,
            "run_dir": str(output_dir) if output_dir else "",
            "updated_at": _utc_now_iso(),
        }

    @classmethod
    def _recompute_aggregates(cls, entry: dict[str, Any]) -> None:
        """Recompute entry-level aggregates from stored task slots (in place).

        Legacy entries without slots keep their last-known aggregates.
        critic ``audit_bundle`` lives on a 0~100 scale, every other task on
        0~1, mirroring the original normalisation.

        Capability is milestone-based with B down-weighted: B re-tests a
        subset of A tasks under scaffolding, so each B milestone counts
        ``B_WEIGHT`` of an A milestone. Full board = 66 + 50×``B_WEIGHT``
        effective points. ``follow_gain`` is the pure following signal:
        mean(B_reward − A_reward) over tasks holding both slots.
        """
        W = cls.B_WEIGHT
        cls._bind_catalog()
        tasks = entry.get("tasks") or {}
        if not tasks:
            pts_t = int(entry.get("scoring_points_total", 0) or 0)
            pts_p = int(entry.get("scoring_points_passed", 0) or 0)
            if pts_t:
                pct = round(pts_p / pts_t * 100.0, 1)
                entry["scoring_points_pct"] = pct
                entry["capability_index"] = pct
            return

        def _collect(condition: str) -> list[dict[str, Any]]:
            roster = cls.CANONICAL_TASKS if condition == "a" else cls.CANONICAL_B_TASKS
            out: list[dict[str, Any]] = []
            for task_id in roster:
                slot = tasks.get(cls.slot_key(task_id, condition))
                if not isinstance(slot, dict):
                    continue
                stored = slot.get("condition")
                if stored and stored != condition:
                    continue
                out.append(slot)
            return out

        a_slots = _collect("a")
        b_slots = _collect("b")
        if not a_slots and not b_slots:
            return

        def _total(slots: list[dict[str, Any]], task_ids: list[str]) -> str:
            sel = [s for s in slots if s.get("task_id") in task_ids]
            if not sel:
                return "-"
            return f"{sum(float(s.get('reward', 0.0)) for s in sel):.2f}"

        # Capability: A milestones at full value, B milestones at B_WEIGHT.
        # Displayed points are the weighted effective points (1 decimal).
        a_p = sum(int(s.get("milestones_passed", 0)) for s in a_slots)
        a_t = sum(int(s.get("milestones_total", 0)) for s in a_slots)
        b_p = sum(int(s.get("milestones_passed", 0)) for s in b_slots)
        b_t = sum(int(s.get("milestones_total", 0)) for s in b_slots)
        eff_p = round(a_p + W * b_p, 1)
        eff_t = round(a_t + W * b_t, 1)
        entry["scoring_points_passed"] = eff_p
        entry["scoring_points_total"] = eff_t
        entry["scoring_points_pct"] = round(eff_p / eff_t * 100.0, 1) if eff_t else 0.0
        entry["capability_index"] = entry["scoring_points_pct"]
        entry["total_tokens"] = sum(int(s.get("total_tokens", 0)) for s in a_slots + b_slots)
        entry["a_scoring_points_passed"] = a_p
        entry["a_scoring_points_total"] = a_t
        for family, field in cls.SUITE_SCORE_FIELDS.items():
            task_ids = list(cls.SUITE_TASKS.get(family, ()))
            if family == "critic":
                critic = next((s for s in a_slots if s.get("task_id") in task_ids), None)
                entry[field] = f"{float(critic.get('reward', 0.0)):.1f}" if critic else "-"
            else:
                entry[field] = _total(a_slots, task_ids)
        for family, field in cls.SUITE_B_SCORE_FIELDS.items():
            entry[field] = _total(b_slots, list(cls.SUITE_B_TASKS.get(family, ())))
        entry["b_scoring_points_passed"] = sum(int(s.get("milestones_passed", 0)) for s in b_slots)
        entry["b_scoring_points_total"] = sum(int(s.get("milestones_total", 0)) for s in b_slots)
        b_t = entry["b_scoring_points_total"]
        b_p = entry["b_scoring_points_passed"]
        entry["b_scoring_points_pct"] = round(b_p / b_t * 100.0, 1) if b_t else 0.0
        # 遵循增益：同时持有 A/B 槽的任务上 (B−A) 奖励均值；无成对槽位记 None。
        a_by_task = {s.get("task_id"): s for s in a_slots}
        gains: list[float] = []
        for s in b_slots:
            a_slot = a_by_task.get(s.get("task_id"))
            if isinstance(a_slot, dict):
                gains.append(float(s.get("reward", 0.0)) - float(a_slot.get("reward", 0.0)))
        entry["follow_gain"] = round(sum(gains) / len(gains), 2) if gains else None
        entry["follow_gain_n"] = len(gains)
        n_a = len(cls.CANONICAL_TASKS)
        n_b = len(cls.CANONICAL_B_TASKS)
        entry["tasks_covered"] = f"A {len(a_slots)}/{n_a} · B {len(b_slots)}/{n_b}"
        trace_pool = a_slots or b_slots
        latest = max(trace_pool, key=lambda s: str(s.get("updated_at", "")))
        entry["run_dir"] = str(latest.get("run_dir", ""))

    @classmethod
    def update_leaderboard(
        cls,
        reports: list[EvaluationReport],
        model_id: str,
        driver: str,
        effort: str | None = None,
        output_dir: Path | None = None,
        wall_time: float = 0.0,
        on_regress: str = "keep-best",
        ask_fn: Any = None,
    ) -> Path:
        """Merge a completed run into the master leaderboard.

        Reports are decomposed into per-task best slots: a slot is replaced
        only when the new reward meets or beats the stored one, so partial
        (single-task) reruns accumulate instead of wiping the row.

        ``on_regress`` governs regressed slots (new reward strictly below
        the stored best): ``"keep-best"`` (default) silently keeps the
        stored slot, ``"overwrite"`` always replaces, ``"ask"`` delegates to
        ``ask_fn(scope, old_slot, new_slot) -> bool`` (True = overwrite;
        a missing/false answer keeps the best, never blocks headless runs).
        """
        cls._bind_catalog()
        if not reports:
            return LEADERBOARD_MD_PATH

        if on_regress not in ("keep-best", "overwrite", "ask"):
            on_regress = "keep-best"

        entry_key = f"{model_id}@{effort or 'default'}"
        with exclusive_file_lock(LEADERBOARD_JSON_PATH):
            data = cls.load_data()
            entry = data.get(entry_key)
            if not isinstance(entry, dict):
                entry = {"model_id": model_id, "driver": driver,
                         "effort": effort or "default", "tasks": {}}
            if not isinstance(entry.get("tasks"), dict):
                entry["tasks"] = {}
            entry["driver"] = driver  # latest writer wins (slots keep their own)

            for report in reports:
                task_id = report.task_id
                condition = getattr(report, "condition", "a") or "a"
                if task_id not in cls.CANONICAL_TASKS:
                    continue
                # B slots use task@b so they never clobber the A task slot.
                slot_key = task_id if condition == "a" else f"{task_id}@{condition}"
                new_slot = cls._slot_from_report(report, driver, output_dir)
                old_slot = entry["tasks"].get(slot_key)
                if old_slot is None:
                    entry["tasks"][slot_key] = new_slot
                    continue
                new_r = float(new_slot.get("reward", 0.0))
                old_r = float(old_slot.get("reward", 0.0))
                if new_r + cls.REWARD_EPS >= old_r:
                    entry["tasks"][slot_key] = new_slot
                    continue
                overwrite = on_regress == "overwrite"
                if on_regress == "ask" and callable(ask_fn):
                    try:
                        overwrite = bool(ask_fn(
                            f"{model_id} · {slot_key}",
                            old_slot, new_slot,
                        ))
                    except Exception:
                        overwrite = False
                if overwrite:
                    entry["tasks"][slot_key] = new_slot

            cls._recompute_aggregates(entry)
            entry["wall_time_seconds"] = round(wall_time, 1)
            entry["updated_at"] = _utc_now_iso()
            data[entry_key] = entry
            cls.save_data(data)
            cls.export_markdown(data, already_locked=True)
        return LEADERBOARD_MD_PATH

    @classmethod
    def slot_key(cls, task_id: str, condition: str = "a") -> str:
        """A slots are bare task ids; B (and later) slots are ``task@condition``."""
        cond = (condition or "a").lower()
        return task_id if cond == "a" else f"{task_id}@{cond}"

    @classmethod
    def format_slot_reward(cls, task_id: str, reward: float) -> str:
        cls._bind_catalog()
        if task_id in cls.SUITE_TASKS.get("critic", ()):
            return f"{float(reward):.1f} / 100"
        return f"{float(reward):.2f} / 1.00"

    @classmethod
    def sorted_entries(cls, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Master ranking over the stored rows (no second scoring pass)."""
        cls._bind_catalog()
        data = data if data is not None else cls.load_data()
        entries = [e for e in data.values() if isinstance(e, dict)]
        return sorted(
            entries,
            key=lambda x: (
                float(x.get("capability_index", 0.0) or 0.0),
                float(x.get("scoring_points_pct", 0.0) or 0.0),
                -int(x.get("total_tokens", 0) or 0),
            ),
            reverse=True,
        )

    @classmethod
    def task_board(
        cls,
        task_id: str,
        data: dict[str, Any] | None = None,
        condition: str | None = "a",
    ) -> list[dict[str, Any]]:
        """Rank master rows by one stored task slot.

        This is a sort view of ``leaderboard.json``, not a separate table:
        slot reward / milestones / tokens are read as stored; ``capability_index``
        is the same row's already-computed aggregate (secondary key).
        ``condition=None`` emits A and B slots as distinct rows.
        """
        cls._bind_catalog()
        data = data if data is not None else cls.load_data()
        conditions = ["a", "b"] if condition is None else [condition or "a"]
        rows: list[dict[str, Any]] = []
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            tasks = entry.get("tasks")
            if not isinstance(tasks, dict):
                continue
            for cond in conditions:
                slot = tasks.get(cls.slot_key(task_id, cond))
                if not isinstance(slot, dict):
                    continue
                raw_tokens = slot.get("total_tokens")
                try:
                    tokens_i = int(raw_tokens) if raw_tokens is not None else None
                except (TypeError, ValueError):
                    tokens_i = None
                rows.append({
                    "entry_key": key,
                    "model_id": entry.get("model_id", "?"),
                    "driver": slot.get("driver") or entry.get("driver", "?"),
                    "effort": entry.get("effort", "default"),
                    "condition": cond,
                    "reward": float(slot.get("reward", 0.0)),
                    "passed": bool(slot.get("passed", False)),
                    "milestones": (
                        f"{int(slot.get('milestones_passed', 0))}"
                        f"/{int(slot.get('milestones_total', 0))}"
                    ),
                    "total_tokens": tokens_i,
                    "capability_index": float(entry.get("capability_index", 0.0) or 0.0),
                    "run_dir": str(slot.get("run_dir", "")),
                    "updated_at": str(slot.get("updated_at", "")),
                    "legacy": False,
                })
        rows.sort(
            key=lambda r: (
                r["reward"],
                r["capability_index"],
                -(r["total_tokens"] if r["total_tokens"] is not None else 10 ** 18),
            ),
            reverse=True,
        )
        return rows

    @classmethod
    def render_markdown(cls, data: dict[str, Any] | None = None) -> str:
        """Build LEADERBOARD.md text from one JSON snapshot (no extra scoring)."""
        cls._bind_catalog()
        data = data if data is not None else cls.load_data()
        sorted_entries = cls.sorted_entries(data)

        medals = ["👑 1", "🥈 2", "🥉 3"]
        now_str = _utc_now_iso()

        lines = [
            "# 🏆 Benchmark v3 全维度权威榜单 (Master Leaderboard)",
            "",
            f"> **最新更新**: `{now_str}`  ",
            "> **评分点**: A 里程碑全额，B 里程碑按权重 `0.2` 折算；A 最多 66（short 30 · long 20 · reviewer 12 · critic 4），B 最多 50×0.2=10，满测 **76** 有效分  ",
            "> **综合指数**: `有效得分 / 有效总数 × 100`（和通过率同一口径）  ",
            "> **遵循增益**: 同任务 `(B−A)` 奖励均值；正值=吃到脚手架红利，零/负=给菜谱也白给  ",
            "> **总榜排序**: 综合指数降序 ➔ 评分点通过率降序 ➔ Token 消耗升序  ",
            "> **合并口径**: 每模型每档 effort 保留各分任务历史最高分；A/B 槽位互不覆盖  ",
            "> **分任务榜**: 同源 `leaderboard.json`，按该任务槽位重排；**不是**独立计分表  ",
            "",
            "| 排名 | 模型标识 (Model ID) | 驱动 / 思考强度 | 综合指数 | 评分点 | 覆盖 | 遵循增益 | critic (/100) | reviewer (/3) | short A (/3) | short B (/3) | long A (/2) | long B (/2) | Token | 耗时 | 制品 |",
            "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | ---: | ---: | :---: |",
        ]

        for i, item in enumerate(sorted_entries):
            rank_str = medals[i] if i < len(medals) else str(i + 1)
            m_id = item.get("model_id", "unknown")
            drv = item.get("driver", "openai")
            eff = item.get("effort", "default")
            cap = float(item.get("capability_index", 0.0) or 0.0)
            pts_p = item.get("scoring_points_passed", 0)
            pts_t = item.get("scoring_points_total", 66)
            pts_pct = item.get("scoring_points_pct", 0.0)
            covered = item.get("tasks_covered", "-")
            gain = item.get("follow_gain")
            gain_s = f"{gain:+.2f} (n={item.get('follow_gain_n', 0)})" if gain is not None else "-"
            c_sc = item.get("critic_score", "-")
            rev_sc = item.get("reviewer_score", "-")
            sh_sc = item.get("short_score", "-")
            sh_b = item.get("short_b_score", "-")
            lg_sc = item.get("long_score", "-")
            lg_b = item.get("long_b_score", "-")
            tokens = item.get("total_tokens", 0)
            wt = item.get("wall_time_seconds", 0.0)
            r_dir = item.get("run_dir", "")
            link = f"[查看日志]({r_dir})" if r_dir else "-"

            lines.append(
                f"| {rank_str} | **`{m_id}`** | `{drv}` · `{eff}` | **`{cap:.1f} / 100`** | "
                f"**`{pts_p}/{pts_t}`** (`{pts_pct:.1f}%`) | `{covered}` | `{gain_s}` | "
                f"`{c_sc}` | `{rev_sc}` | `{sh_sc}` | `{sh_b}` | `{lg_sc}` | `{lg_b}` | "
                f"`{tokens:,}` | `{wt:.1f}s` | {link} |"
            )

        lines.append("")
        lines.extend(cls._task_board_markdown_sections(data))
        lines.append("---")
        lines.append("*由 Benchmark v3 自动化轻量 Harness 驱动，每次评测完成自动增量对齐落盘。*")
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def _task_board_markdown_sections(cls, data: dict[str, Any]) -> list[str]:
        cls._bind_catalog()
        lines = [
            "---",
            "",
            "## 分任务榜（同源总榜，按该任务得分排序）",
            "",
            "> 下列各表**不重新计分**：行来自上方同一份 JSON，主键是该任务已存槽位得分，次键是该行已存的综合能力指数。",
            "",
        ]
        medals = ["👑 1", "🥈 2", "🥉 3"]
        any_rows = False
        for task_id in cls.CANONICAL_TASKS:
            for cond in ("a", "b"):
                rows = cls.task_board(task_id, data=data, condition=cond)
                if not rows:
                    continue
                any_rows = True
                label = task_id if cond == "a" else f"{task_id}@b"
                lines.append(f"### `{label}`")
                lines.append("")
                lines.append(
                    "| 排名 | 模型标识 | 驱动 / 强度 | 条件 | 该任务得分 | 通过 | 综合指数（同行） | Token | 更新 |"
                )
                lines.append(
                    "| :---: | :--- | :---: | :---: | ---: | :---: | ---: | ---: | :--- |"
                )
                for i, row in enumerate(rows):
                    rank_str = medals[i] if i < len(medals) else str(i + 1)
                    tokens = row["total_tokens"]
                    token_s = f"{tokens:,}" if tokens is not None else "-"
                    updated = str(row.get("updated_at") or "")[:10] or "-"
                    lines.append(
                        f"| {rank_str} | **`{row['model_id']}`** | "
                        f"`{row['driver']}` · `{row['effort']}` | `{row['condition']}` | "
                        f"`{cls.format_slot_reward(task_id, row['reward'])}` | "
                        f"{'✔' if row['passed'] else '✖'} | "
                        f"`{row['capability_index']:.1f}` | `{token_s}` | `{updated}` |"
                    )
                lines.append("")
        if not any_rows:
            lines.append("*暂无分任务槽位。跑完评测后由总榜自动生成。*")
            lines.append("")
        return lines

    @classmethod
    def refresh_aggregates(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Recompute A/B split fields on every row that has task slots."""
        for entry in data.values():
            if isinstance(entry, dict):
                cls._recompute_aggregates(entry)
        return data

    @classmethod
    def ingest_run_dir(
        cls,
        run_dir: str | Path,
        *,
        model_id: str | None = None,
        driver: str | None = None,
        effort: str | None = None,
        wall_time: float | None = None,
    ) -> Path:
        """Merge every ``evaluation.json`` under a run directory into the master board."""
        import json

        root = Path(run_dir)
        reports: list[EvaluationReport] = []
        for path in sorted(root.glob("**/evaluation.json")):
            try:
                reports.append(
                    EvaluationReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except Exception:
                continue
        if not reports:
            return LEADERBOARD_MD_PATH
        summary: dict[str, Any] = {}
        summary_path = root / "summary.json"
        if summary_path.is_file():
            try:
                loaded = json.loads(summary_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    summary = loaded
            except Exception:
                summary = {}
        mid = model_id or str(summary.get("model_id") or reports[0].model_id)
        drv = driver or str(summary.get("driver") or "openai")
        eff = effort if effort is not None else summary.get("effort")
        wt = wall_time if wall_time is not None else summary.get("wall_time_seconds") or 0.0
        return cls.update_leaderboard(
            reports,
            model_id=mid,
            driver=drv,
            effort=str(eff) if eff else None,
            output_dir=root,
            wall_time=float(wt),
        )

    @classmethod
    def export_markdown(
        cls,
        data: dict[str, Any] | None = None,
        *,
        already_locked: bool = False,
    ) -> str:
        persist = data is None

        def _write(payload: dict[str, Any]) -> str:
            cls.refresh_aggregates(payload)
            md_content = cls.render_markdown(payload)
            try:
                LEADERBOARD_MD_PATH.write_text(md_content, encoding="utf-8")
            except OSError:
                pass
            if persist:
                cls.save_data(payload)
            return md_content

        if persist and not already_locked:
            with exclusive_file_lock(LEADERBOARD_JSON_PATH):
                return _write(cls.load_data())
        payload = cls.load_data() if persist else data
        return _write(payload)

    @classmethod
    def suite_board(cls, suite: str, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Suite drill-down: same JSON, primary key = stored slot sum for that family.

        Not a second scoring pipeline. Legacy rows without ``tasks`` are tagged
        ``legacy=True`` and fall back to the last stored suite field.
        """
        cls._bind_catalog()
        data = data if data is not None else cls.load_data()
        task_ids = list(cls.SUITE_TASKS.get(suite, ()))
        score_field = cls.SUITE_SCORE_FIELDS.get(suite, "")
        rows: list[dict[str, Any]] = []
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            slots = [s for t, s in (entry.get("tasks") or {}).items()
                     if t in task_ids and isinstance(s, dict)]
            if slots:
                total = round(sum(float(s.get("reward", 0.0)) for s in slots), 2)
                rows.append({
                    "entry_key": key,
                    "model_id": entry.get("model_id", "?"),
                    "driver": entry.get("driver", "?"),
                    "effort": entry.get("effort", "default"),
                    "reward": total,
                    "passed": all(bool(s.get("passed", False)) for s in slots),
                    "milestones": f"{sum(int(s.get('milestones_passed', 0)) for s in slots)}"
                                  f"/{sum(int(s.get('milestones_total', 0)) for s in slots)}",
                    "total_tokens": sum(int(s.get("total_tokens", 0)) for s in slots),
                    "capability_index": float(entry.get("capability_index", 0.0) or 0.0),
                    "run_dir": str(max(slots, key=lambda s: str(s.get("updated_at", ""))).get("run_dir", "")),
                    "updated_at": str(max(str(s.get("updated_at", "")) for s in slots)),
                    "legacy": False,
                })
                continue
            raw = entry.get(score_field, "-") if score_field else "-"
            try:
                legacy_total = float(raw)
            except (TypeError, ValueError):
                continue
            rows.append({
                "entry_key": key,
                "model_id": entry.get("model_id", "?"),
                "driver": entry.get("driver", "?"),
                "effort": entry.get("effort", "default"),
                "reward": legacy_total,
                "passed": False,
                "milestones": "-",
                "total_tokens": None,
                "capability_index": float(entry.get("capability_index", 0.0) or 0.0),
                "run_dir": entry.get("run_dir", ""),
                "updated_at": entry.get("updated_at", ""),
                "legacy": True,
            })
        rows.sort(
            key=lambda r: (
                r["reward"],
                r.get("capability_index", 0.0),
                -(r["total_tokens"] if r["total_tokens"] is not None else 10 ** 18),
            ),
            reverse=True,
        )
        return rows


def self_test() -> tuple[int, int]:
    """In-memory ranking views; does not write LEADERBOARD.md."""
    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} report::{name}", flush=True)

    data = {
        "low@high": {
            "model_id": "low",
            "driver": "openai",
            "effort": "high",
            "capability_index": 90.0,
            "scoring_points_pct": 80.0,
            "scoring_points_passed": 8,
            "scoring_points_total": 10,
            "total_tokens": 10,
            "tasks_covered": "1/9",
            "critic_score": "-",
            "reviewer_score": "-",
            "short_score": "0.40",
            "long_score": "-",
            "wall_time_seconds": 1.0,
            "run_dir": "",
            "tasks": {
                "varint_parser": {
                    "task_id": "varint_parser",
                    "reward": 0.4,
                    "passed": False,
                    "milestones_passed": 4,
                    "milestones_total": 10,
                    "total_tokens": 100,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "driver": "openai",
                    "run_dir": "",
                }
            },
        },
        "high@high": {
            "model_id": "high",
            "driver": "openai",
            "effort": "high",
            "capability_index": 50.0,
            "scoring_points_pct": 40.0,
            "scoring_points_passed": 4,
            "scoring_points_total": 10,
            "total_tokens": 20,
            "tasks_covered": "1/9",
            "critic_score": "-",
            "reviewer_score": "-",
            "short_score": "1.00",
            "long_score": "-",
            "wall_time_seconds": 2.0,
            "run_dir": "",
            "tasks": {
                "varint_parser": {
                    "task_id": "varint_parser",
                    "reward": 1.0,
                    "passed": True,
                    "milestones_passed": 10,
                    "milestones_total": 10,
                    "total_tokens": 200,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "driver": "openai",
                    "run_dir": "",
                },
                "varint_parser@b": {
                    "task_id": "varint_parser",
                    "reward": 0.2,
                    "passed": False,
                    "milestones_passed": 2,
                    "milestones_total": 10,
                    "total_tokens": 50,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "driver": "openai",
                    "run_dir": "",
                },
            },
        },
    }
    master = MasterLeaderboard.sorted_entries(data)
    check("master_sorts_by_capability", master[0]["model_id"] == "low")
    rows = MasterLeaderboard.task_board("varint_parser", data=data, condition="a")
    check("task_board_primary_is_slot", rows[0]["model_id"] == "high")
    check("task_board_keeps_row_capability", rows[0]["capability_index"] == 50.0)
    check("task_board_second_is_low", rows[1]["model_id"] == "low")
    b_rows = MasterLeaderboard.task_board("varint_parser", data=data, condition="b")
    check("task_board_b_slot", len(b_rows) == 1 and abs(b_rows[0]["reward"] - 0.2) < 1e-9)
    both = MasterLeaderboard.task_board("varint_parser", data=data, condition=None)
    check("task_board_both_conditions", len(both) == 3)
    suite = MasterLeaderboard.suite_board("short", data=data)
    check("suite_board_same_slot_sum", suite[0]["model_id"] == "high" and suite[0]["reward"] == 1.0)
    md = MasterLeaderboard.render_markdown(data)
    check("md_has_master", "权威榜单" in md)
    check("md_has_task_view", "分任务榜" in md and "`varint_parser`" in md)
    check("md_explains_derived", "不是**独立计分" in md or "不是独立" in md)
    check("slot_key_b", MasterLeaderboard.slot_key("varint_parser", "b") == "varint_parser@b")
    MasterLeaderboard._bind_catalog()
    check("catalog_bound", len(MasterLeaderboard.CANONICAL_TASKS) == 9)
    mixed = {
        "model_id": "mix",
        "driver": "openai",
        "effort": "high",
        "tasks": {
            "varint_parser": {
                "task_id": "varint_parser",
                "condition": "a",
                "reward": 0.4,
                "passed": False,
                "milestones_passed": 4,
                "milestones_total": 10,
                "total_tokens": 10,
                "updated_at": "2026-01-01T00:00:00Z",
            },
            "varint_parser@b": {
                "task_id": "varint_parser",
                "condition": "b",
                "reward": 1.0,
                "passed": True,
                "milestones_passed": 10,
                "milestones_total": 10,
                "total_tokens": 99,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        },
    }
    MasterLeaderboard._recompute_aggregates(mixed)
    # A 4/10 全额 + B 10/10 按 0.2 折算 → 有效 6.0/12.0 = 50.0；增益 +0.60
    check("points_weighted_b", mixed["scoring_points_passed"] == 6.0)
    check("points_total_weighted", mixed["scoring_points_total"] == 12.0)
    check("capability_is_weighted", abs(float(mixed["capability_index"]) - 50.0) < 0.15)
    check("b_points_still_tracked", mixed["b_scoring_points_passed"] == 10)
    check("follow_gain_is_b_minus_a", abs(float(mixed["follow_gain"]) - 0.6) < 1e-9)
    check("coverage_splits_ab", "A 1/" in mixed["tasks_covered"] and "B 1/" in mixed["tasks_covered"])
    check("md_explains_ratio", "有效得分" in md and "76" in md)
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
