"""
Fast offline rescore for short / reviewer / critic slots (0 LLM tokens).

Usage::

    python rescore_fast.py               # all three fast suites
    python rescore_fast.py short reviewer  # selected subsets

Long (raft/saga) slots are handled separately by ``rescore_long.py`` — each
long re-run spawns a Jepsen scenario and takes tens of seconds.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "D:/vscode/kimisubagentexplore/subagentbenchmark")

from benchmark_v3.bench_harness.core.report import MasterLeaderboard
from benchmark_v3.bench_harness.suites.critic import (
    BAIT_POINTS,
    DEPTH_POINTS,
    FIXTURES,
    POINTS_PER_FLAW,
    RECALL_POINTS,
    score_audit,
)
from benchmark_v3.bench_harness.suites.reviewer import ReviewerSuite
from benchmark_v3.bench_harness.suites.short_task import run_task_checks

FAST_SUITES = {"short", "short_b", "reviewer", "critic"}

#: ``judge_model`` values that identify a heuristic / unknown scorer rather
#: than an actual LLM Judge verdict.
_NON_LLM_JUDGE_MODELS = {None, "", "unknown", "heuristic", "strict_heuristic_fallback"}


def _is_actual_llm_verdict(stored: object) -> bool:
    """True when ``judge_verdict.json`` carries a real LLM Judge verdict.

    Written by ``critic.py`` from ``score_audit`` (``provisional`` is True iff
    ``judge_driver is None``). An actual LLM verdict requires the file to
    parse as a dict, ``provisional`` to be explicitly false, and a usable
    ``judge_model`` name (an explicitly ``provisional: false`` file is
    trusted even when the model name is odd — e.g. legacy rows without one).
    Heuristic rows written before this field existed have
    ``judge_model == 'strict_heuristic_fallback'`` and no ``provisional`` key.
    """
    if not isinstance(stored, dict):
        return False
    if stored.get("provisional", True) is not False:
        return False
    return stored.get("judge_model") not in _NON_LLM_JUDGE_MODELS


def _verdict_num(stored: dict, key: str) -> float:
    """Float out of a verdict field (JSON null / garbage -> 0.0, never raises)."""
    try:
        return float(stored.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _recall_from_verdict(stored: dict) -> float | None:
    """Recover expert recall from the LLM verdict without re-running matching.

    1. A numeric ``recall`` field (not currently written) wins verbatim.
    2. Else ``verdicts`` (file -> {level, score}): every flaw the judge
       graded above L0 was recalled and matched, worth ``POINTS_PER_FLAW``
       (55/4 = 13.75) each, clamped to [0, 55].
    Returns ``None`` when neither source is usable.
    """
    recall = stored.get("recall")
    if isinstance(recall, (int, float)) and not isinstance(recall, bool):
        return round(min(RECALL_POINTS, max(0.0, float(recall))), 2)
    verdicts = stored.get("verdicts")
    if not isinstance(verdicts, dict) or not verdicts:
        return None
    recalled = sum(
        1 for v in verdicts.values()
        if isinstance(v, dict) and str(v.get("level", "L0")).upper() != "L0"
    )
    return round(min(RECALL_POINTS, max(0.0, recalled * POINTS_PER_FLAW)), 2)


def rescore_short(task_id: str, ws_dir: Path) -> dict:
    res = run_task_checks(task_id, ws_dir)
    assertions = res.get("assertions", [])
    passed_n = sum(1 for a in assertions if a.get("passed"))
    total_n = len(assertions)
    reward = (passed_n / total_n) if total_n else 0.0
    return {
        "reward": round(reward, 2),
        "passed": passed_n == total_n,
        "milestones_passed": passed_n,
        "milestones_total": total_n,
    }


def rescore_reviewer(task_id: str, ws_dir: Path) -> dict:
    suite = ReviewerSuite()
    milestones, _extras = suite.evaluate_task(task_id, ws_dir, None)
    passed_n = sum(1 for m in milestones if m.passed)
    total_n = len(milestones)
    reward = (sum(m.score for m in milestones) / total_n) if total_n else 0.0
    return {
        "reward": round(reward, 2),
        "passed": passed_n == total_n,
        "milestones_passed": passed_n,
        "milestones_total": total_n,
    }


def rescore_critic(_task_id: str, ws_dir: Path) -> dict:
    """Critic rescore: deterministic parts recomputed; LLM-judged parts preserved.

    When ``judge_verdict.json`` holds an *actual* LLM Judge verdict
    (``provisional`` false + a real ``judge_model``), the expert's recall and
    depth scores are preserved verbatim — the offline keyword heuristic
    (``CriticJudgeEvaluator(judge_driver=None)`` / ``_score_depth_heuristic``)
    must never downgrade or inflate them. Novel bonus stays from the verdict
    too; bait and format stay deterministic from ``audit.json``. Missing,
    unreadable, or provisional/heuristic verdict files keep the fully
    recomputed fallback. Rescoring never calls a live judge (0 LLM tokens).
    """
    audit_file = ws_dir / "audit.json"
    judge_path = ws_dir / "judge_verdict.json"
    stored = None
    if judge_path.is_file():
        try:
            stored = json.loads(judge_path.read_text(encoding="utf-8"))
        except Exception:
            stored = None

    if not audit_file.is_file():
        if _is_actual_llm_verdict(stored):
            depth = _verdict_num(stored, "total_depth")
            novel = _verdict_num(stored, "novel_bonus")
            recall = _recall_from_verdict(stored)
            if recall is not None:
                total = min(100.0, round(recall + 20.0 + depth + 0.0 + novel, 2))
                ms = 1 + (1 if depth >= 14 else 0) + (1 if recall >= 2 * POINTS_PER_FLAW else 0)
                return {"reward": total, "passed": total >= 70.0, "milestones_passed": ms,
                        "milestones_total": 4}
        # 交白卷 fallback (provisional rows carry no trustworthy recall
        # evidence, so bait-only + stored depth/novel stands)
        depth = float(stored.get("total_depth", 0.0)) if stored else 0.0
        novel = float(stored.get("novel_bonus", 0.0)) if stored else 0.0
        total = round(20.0 + depth + novel, 2)
        return {"reward": total, "passed": total >= 70.0, "milestones_passed": 1, "milestones_total": 4}

    try:
        findings = json.loads(audit_file.read_text(encoding="utf-8"))
    except Exception:
        findings = []
    if not isinstance(findings, list) or len(findings) == 0:
        findings = []
        format_ok = False
    else:
        format_ok = True

    codebase = {name: src for name, src in FIXTURES.items()}
    scores = score_audit(findings, codebase, format_ok=format_ok, judge_driver=None)

    if _is_actual_llm_verdict(stored):
        # 保留专家裁判的 recall / depth / novel：离线重算的启发式不得
        # 降级或抬高已由 LLM Judge 判定的分数。
        depth = _verdict_num(stored, "total_depth")
        novel = _verdict_num(stored, "novel_bonus")
        recall = _recall_from_verdict(stored)
        if recall is None:
            # verdict file unusable for recall (e.g. empty verdicts):
            # fall back to the freshly recomputed deterministic recall.
            recall = float(scores["recall"])
        recall = min(RECALL_POINTS, max(0.0, float(recall)))
        format_score = float(scores.get("format", 0.0))  # deterministic from audit.json
        bait = float(scores.get("bait", 0.0))            # deterministic from audit.json
    elif stored is not None:
        # 存在 verdict 文件但属启发式/临时评分：depth/novel 取本次重算，
        # 与原评测的启发式兜底口径一致。
        depth = float(scores.get("depth", 0.0))
        novel = float(scores.get("novel_bonus", 0.0))
        recall = float(scores.get("recall", 0.0))
        bait = float(scores.get("bait", 0.0))
        format_score = float(scores.get("format", 0.0))
    else:
        # 无 verdict 文件：depth/novel/recall 全部取本次重算
        depth = float(scores.get("depth", 0.0))
        novel = float(scores.get("novel_bonus", 0.0))
        recall = float(scores.get("recall", 0.0))
        bait = float(scores.get("bait", 0.0))
        format_score = float(scores.get("format", 0.0))

    total = min(100.0, round(recall + bait + depth + format_score + novel, 2))
    # 里程碑阈值必须与 critic.py 的实时判分保持一致，否则同一份分数在
    # 实时评测与离线复算下会算出不同的 milestones_passed。
    # critic.py: recall >= POINTS_PER_FLAW * 2, depth >= DEPTH_POINTS / 2
    ms = 0
    ms += 1 if recall >= POINTS_PER_FLAW * 2 else 0
    ms += 1 if bait >= BAIT_POINTS else 0
    ms += 1 if depth >= DEPTH_POINTS / 2 else 0
    ms += 1 if format_score > 0 else 0
    return {"reward": total, "passed": total >= 70.0, "milestones_passed": ms, "milestones_total": 4}


HANDLERS = {
    "short": rescore_short,
    "short_b": rescore_short,
    "reviewer": rescore_reviewer,
    "critic": rescore_critic,
}


# ---------------------------------------------------------------------------
# Self-test (--self-test): default CLI (no args) keeps rescoring as today.
# ---------------------------------------------------------------------------

def self_test() -> tuple[int, int]:
    """Verify critic rescore fidelity against synthetic workspaces.

    Covers:
    1. Actual LLM verdict  -> recall/depth preserved verbatim, immune to a
       keyword-poor ``audit.json`` that the heuristic would score lower.
    2. Provisional/heuristic verdict -> depth recomputed by the heuristic.
    3. Missing verdict file -> existing fallback (fully recomputed).
    Returns ``(passed, failed)`` counts.
    """
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} rescore::{name}", flush=True)

    def make_ws(audit_findings: list[dict] | str | None) -> Path:
        tmp = tempfile.mkdtemp(prefix="rescore-fast-")
        ws = Path(tmp)
        code = ws / "codebase"
        code.mkdir(parents=True, exist_ok=True)
        for name, src in FIXTURES.items():
            (code / name).write_text(src, encoding="utf-8")
        if audit_findings is not None:
            payload = audit_findings if isinstance(audit_findings, str) else json.dumps(audit_findings)
            (ws / "audit.json").write_text(payload, encoding="utf-8")
        return ws

    def verdict_payload(**over) -> dict:
        base = {
            "judge_model": "deepseek-v4.1-flash",
            "provisional": False,
            "total_depth": 17.5,
            "novel_bonus": 2.5,
            "novel_verdicts": [],
            "verdicts": {
                "session_tokens.py": {"level": "L2", "score": 2.5},
                "archive_import.py": {"level": "L3", "score": 3.5},
                "config_codec.py": {"level": "L0", "score": 0.0},
                "pixel_blend.py": {"level": "L4", "score": 5.0},
            },
        }
        base.update(over)
        return base

    # Keyword-poor audit: matches one flaw line but with a category alias the
    # heuristic recall matcher rejects, so the offline recompute undershoots.
    poor_audit = [
        {"file": "session_tokens.py", "line": 8, "severity": "low",
         "category": "naming", "root_cause": "unclear", "fix": "rename"},
        {"file": "fast_lookup.py", "line": 20, "severity": "high",
         "category": "missing lock", "root_cause": "no mutex", "fix": "add lock"},
    ]

    def format_pts(n_findings: int) -> float:
        return 5.0 if n_findings else 0.0

    # -- 1. actual LLM verdict: recall/depth verbatim, not heuristic-downgraded --
    ws = make_ws(poor_audit)
    (ws / "judge_verdict.json").write_text(json.dumps(verdict_payload()), encoding="utf-8")
    res = rescore_critic("audit_bundle", ws)
    # expected: recall = 3 non-L0 x 13.75 = 41.25 (NOT the heuristic 0-13.75),
    # depth = 17.5 verbatim, novel = 2.5 verbatim, bait = 10, format = 5.
    expect = round(41.25 + 10.0 + 17.5 + format_pts(len(poor_audit)) + 2.5, 2)
    check("llm_recall_preserved", abs(res["reward"] - expect) < 0.011)
    check("llm_reward_is_76_25", res["reward"] == 76.25)
    check("llm_milestones", res["milestones_passed"] == 3 and res["milestones_total"] == 4)

    # -- 1b. LLM verdict wins even with keyword-poor audit AND empty audit dir --
    # (same verdict, audit.json deleted entirely -> still faithful)
    ws = make_ws(None)
    (ws / "judge_verdict.json").write_text(json.dumps(verdict_payload()), encoding="utf-8")
    res = rescore_critic("audit_bundle", ws)
    # no audit.json: bait 20 (no findings), format 0; recall/depth/novel stay.
    expect = round(41.25 + 20.0 + 17.5 + 0.0 + 2.5, 2)
    check("llm_no_audit_recall_preserved", res["reward"] == expect)

    # -- 1c. verdicts-derived recall ignores L0 and clamps to [0, 55] --
    all_l4 = verdict_payload(
        verdicts={f: {"level": "L4", "score": 5.0}
                  for f in ("session_tokens.py", "archive_import.py",
                            "config_codec.py", "pixel_blend.py")}
    )
    ws = make_ws(poor_audit)
    (ws / "judge_verdict.json").write_text(json.dumps(all_l4), encoding="utf-8")
    res = rescore_critic("audit_bundle", ws)
    check("llm_recall_full_55", res["reward"] == round(55.0 + 10.0 + 17.5 + 5.0 + 2.5, 2))

    # -- 2. provisional/heuristic verdict: depth may be recomputed --
    ws = make_ws(poor_audit)
    (ws / "judge_verdict.json").write_text(json.dumps(verdict_payload(provisional=True)), encoding="utf-8")
    res_prov = rescore_critic("audit_bundle", ws)
    ws = make_ws(poor_audit)  # no verdict file -> same fallback path
    res_missing = rescore_critic("audit_bundle", ws)
    check("provisional_matches_fallback", res_prov["reward"] == res_missing["reward"])
    check("provisional_depth_recomputed", res_prov["reward"] < 41.25 + 10.0 + 17.5 + 5.0 + 2.5)

    # heuristic legacy row (no `provisional` key, strict_heuristic_fallback)
    ws = make_ws(poor_audit)
    (ws / "judge_verdict.json").write_text(json.dumps(
        verdict_payload(judge_model="strict_heuristic_fallback",
                        novel_verdicts=[{"file": "session_tokens.py", "bonus_score": 2.5}])),
        encoding="utf-8")
    res_legacy = rescore_critic("audit_bundle", ws)
    check("legacy_heuristic_recomputed", res_legacy["reward"] == res_missing["reward"])

    # -- 3. missing verdict file: existing fallback (recompute everything) --
    ws = make_ws(poor_audit)
    res = rescore_critic("audit_bundle", ws)
    check("missing_fallback_recomputed", res["reward"] == res_missing["reward"])

    # -- guard: heuristic judge_model names never count as LLM verdicts --
    for bad_model in (None, "", "unknown", "heuristic"):
        check("nonllm_model_%s" % repr(bad_model),
              not _is_actual_llm_verdict(verdict_payload(judge_model=bad_model)))
    check("nonllm_missing_provisional",
          not _is_actual_llm_verdict({"judge_model": "deepseek-v4.1-flash"}))
    check("nonllm_not_dict", not _is_actual_llm_verdict(["nope"]))
    check("llm_bool_recall_not_accepted",
          _recall_from_verdict(verdict_payload(recall=True)) == 41.25)  # bool ignored

    return counts[0], counts[1]


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        args = [a for a in sys.argv[1:] if a != "--self-test"]
        passed, failed = self_test()
        print(f"rescore_fast self-test: {passed} passed, {failed} failed", flush=True)
        if failed:
            return 1
        if not args:
            return 0
        wanted = set(args)  # fall through to a real rescore if suites requested
    else:
        wanted = {a for a in sys.argv[1:] if a}
    if not wanted:
        wanted = FAST_SUITES
    return _run_rescore(wanted)


def _run_rescore(wanted: set[str]) -> int:
    MasterLeaderboard._bind_catalog()
    lb_path = Path("bench_runs/leaderboard.json")
    data = json.loads(lb_path.read_text(encoding="utf-8"))

    changes: list[tuple[str, str, float, float]] = []
    skipped: list[str] = []

    for model_key, entry in sorted(data.items()):
        tasks = entry.get("tasks") or {}
        for slot_key, slot in sorted(tasks.items()):
            task_id = slot.get("task_id", slot_key.split("@")[0])
            condition = slot.get("condition", "b" if "@b" in slot_key else "a")
            family = MasterLeaderboard.TASK_SUITES.get(task_id, "")
            suite_name = f"{family}_b" if (condition == "b" and family in ("short", "long")) else family
            if suite_name not in wanted:
                continue
            run_dir = Path(str(slot.get("run_dir", "")))
            ws_dir = run_dir / suite_name / task_id / "workspace"
            if not ws_dir.is_dir():
                ws_dir = run_dir / family / task_id / "workspace"
            if not ws_dir.is_dir():
                skipped.append(f"{model_key} / {slot_key} (no workspace: {ws_dir})")
                continue
            try:
                new_res = HANDLERS[suite_name](task_id, ws_dir)
            except Exception as exc:  # never let one slot kill the sweep
                skipped.append(f"{model_key} / {slot_key} (error: {exc!r})")
                continue
            old_reward = float(slot.get("reward", 0.0))
            new_reward = new_res["reward"]
            if abs(new_reward - old_reward) > 0.001:
                changes.append((model_key, slot_key, old_reward, new_reward))
            # 复算不重跑 agent：原运行的 token/耗时遥测必须继承，否则
            # record_run 的均值会被 0 污染（成本指标与 Succ/Mtok 全废）。
            new_res.setdefault("total_tokens", int(slot.get("total_tokens", 0) or 0))
            new_res.setdefault("wall_seconds", float(slot.get("wall_seconds", 0.0) or 0.0))
            # 并入该 run_dir 的运行历史（就地替换同一次运行），保持均值口径
            MasterLeaderboard.record_run(
                data, model_key, slot_key, new_res, str(run_dir))
        MasterLeaderboard._recompute_aggregates(entry)

    MasterLeaderboard.save_data(data)
    MasterLeaderboard.export_markdown(data)

    print("=" * 62)
    print(f"RESCORE ({', '.join(sorted(wanted))}) — {len(changes)} changed slots")
    print("=" * 62)
    for model_key, slot_key, old, new in changes:
        print(f"  {model_key:32s} {slot_key:22s} {old:>6} -> {new:<6} ({new - old:+.2f})")
    if skipped:
        print("-" * 62)
        print(f"skipped {len(skipped)}:")
        for line in skipped[:12]:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
