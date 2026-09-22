"""Offline rescore for long (raft_cluster / saga_coordinator) slots.

Each evaluable slot is re-run in an ISOLATED temp directory containing only
the model's code file, so stale broker/node state from the original run can
never leak into the fresh Jepsen scenario.

Slots whose code file is missing are left UNTOUCHED (reported as skipped):
an empty workspace means the model delivered nothing and was already scored
0.0; overwriting it would fabricate a result.

Usage::

    python rescore_long.py               # all long slots
    python rescore_long.py k3@max        # one model only
    python rescore_long.py --dry-run     # report only, write nothing

Reconstructed 2026-09-21 after the original untracked copy was lost. Same
contract as before, plus:
- workspace paths resolved via Path.resolve() before spawning children
  (M18: relative paths were being joined against cwd twice)
- UNTOUCHED instead of 0.0 for slots with no deliverable
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "D:/vscode/kimisubagentexplore/subagentbenchmark")

from benchmark_v3.bench_harness.core.report import MasterLeaderboard
from benchmark_v3.bench_harness.suites.long_task import (
    build_raft_milestones,
    build_saga_milestones,
    run_raft_scenario,
    run_saga_scenario,
)

CODE_FILE = {"raft_cluster": "raft.py", "saga_coordinator": "saga.py"}


def find_workspace(run_dir: Path, suite: str, task_id: str) -> Path | None:
    """Locate the task workspace, tolerating suite-dir variations."""
    for cand in (run_dir / suite / task_id / "workspace",
                 run_dir / "long" / task_id / "workspace",
                 run_dir / "long_b" / task_id / "workspace"):
        if cand.is_dir():
            return cand
    return None


def rescore_long(task_id: str, ws_dir: Path) -> dict:
    fname = CODE_FILE[task_id]
    src = ws_dir / fname
    if not src.is_file():
        return None  # no deliverable -> leave the recorded score alone

    with tempfile.TemporaryDirectory(prefix="rescore-long-") as tmp:
        isolated = Path(tmp)
        shutil.copyfile(src, isolated / fname)
        if task_id == "raft_cluster":
            data = run_raft_scenario(isolated, seed=7)
            milestones = build_raft_milestones(data)
        else:
            data = run_saga_scenario(isolated, seed=11)
            milestones = build_saga_milestones(data)

    passed_n = sum(1 for m in milestones if m.passed)
    total_n = len(milestones)
    return {
        "reward": round(sum(m.score for m in milestones) / total_n, 2) if total_n else 0.0,
        "passed": passed_n == total_n,
        "milestones_passed": passed_n,
        "milestones_total": total_n,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    only = args[0] if args else None
    MasterLeaderboard._bind_catalog()
    lb_path = Path("bench_runs/leaderboard.json")
    data = json.loads(lb_path.read_text(encoding="utf-8"))

    changes: list[tuple[str, str, float, float]] = []
    skipped: list[str] = []

    for model_key, entry in sorted(data.items()):
        if only and model_key != only:
            continue
        tasks = entry.get("tasks") or {}
        for slot_key, slot in sorted(tasks.items()):
            task_id = slot.get("task_id", slot_key.split("@")[0])
            if task_id not in CODE_FILE:
                continue
            suite = "long_b" if "@b" in slot_key else "long"
            run_dir = Path(str(slot.get("run_dir", "")))
            ws_dir = find_workspace(run_dir, suite, task_id)
            if ws_dir is None:
                skipped.append(f"{model_key} / {slot_key} (no workspace)")
                continue
            print(f"[eval] {model_key} / {slot_key}", flush=True)
            try:
                new_res = rescore_long(task_id, ws_dir)
            except Exception as exc:
                print(f"       ERROR {type(exc).__name__}: {exc}", flush=True)
                skipped.append(f"{model_key} / {slot_key} (error)")
                continue
            if new_res is None:
                print(f"       skipped (no {CODE_FILE[task_id]})", flush=True)
                skipped.append(f"{model_key} / {slot_key} (no deliverable)")
                continue
            old_reward = float(slot.get("reward", 0.0))
            new_reward = new_res["reward"]
            if abs(new_reward - old_reward) > 0.001:
                changes.append((model_key, slot_key, old_reward, new_reward))
                print(f"       {old_reward} -> {new_reward} ({new_reward - old_reward:+.2f})", flush=True)
            else:
                print(f"       {old_reward} (unchanged)", flush=True)
            if not dry:
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
        if not dry:
            MasterLeaderboard._recompute_aggregates(entry)

    if not dry:
        MasterLeaderboard.save_data(data)
        MasterLeaderboard.export_markdown(data)

    print("=" * 62)
    print(f"LONG RESCORE — {len(changes)} changed slots" + (" (dry-run)" if dry else ""))
    for model_key, slot_key, old, new in changes:
        print(f"  {model_key:32s} {slot_key:22s} {old:>6} -> {new:<6} ({new - old:+.2f})")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for line in skipped[:12]:
            print("  " + line)
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
