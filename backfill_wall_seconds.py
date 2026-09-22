#!/usr/bin/env python3
"""Backfill per-slot ``wall_seconds`` and ``completion_tokens`` from each run
dir's evaluation.json.

The board only ever stored the LAST run's wall time at entry level, which
made any TPS math meaningless (entry tokens span several run dirs). Every
task's ``evaluation.json`` carries ``telemetry.wall_time_seconds`` and
``token_metrics.completion_tokens`` — this script copies both onto the
slot (and its runs[] entries) so the board can aggregate real time, real
generation TPS, and cost ratios. 0 LLM tokens, read-only on the run dirs;
the only write is leaderboard.json + LEADERBOARD.md.

Condition-aware resolution: a B slot's ``suite`` field is the family name
(``long``), so the naive ``<run_dir>/<suite>/<task>`` path resolves to the
A-condition file. Every candidate is validated against the payload's own
``task_id``/``condition`` fields before it is accepted.

Usage::

    python backfill_wall_seconds.py            # backfill missing fields
    python backfill_wall_seconds.py --force    # re-resolve and overwrite all
    python backfill_wall_seconds.py --dry-run  # report coverage only
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "D:/vscode/kimisubagentexplore/subagentbenchmark")

from benchmark_v3.bench_harness.core.report import MasterLeaderboard


def resolve_eval(run_dir: str, task_id: str, condition: str, suite: str) -> Path | None:
    """Locate the evaluation.json for (task, condition), validated by payload.

    B conditions live under ``<suite>_b/``; candidates are checked against
    the file's own ``task_id``/``condition`` so a same-task A file can never
    satisfy a B slot.
    """
    if not run_dir or not task_id:
        return None
    base = Path(run_dir)
    cond = (condition or "a").lower()
    fam = str(suite or "")
    ordered: list[Path] = []
    if cond == "a":
        ordered += [base / fam / task_id]
    else:
        ordered += [base / f"{fam}_b" / task_id, base / fam / task_id]
    # legacy/alternate layouts as last resort
    ordered += [base / f"{fam}_b" / task_id, base / fam / task_id,
                base / "long_b" / task_id, base / "short_b" / task_id]
    seen: set[Path] = set()
    for cand in ordered:
        p = cand / "evaluation.json"
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("task_id")) != task_id:
            continue
        if cond == "b" and str(payload.get("condition", "a")).lower() != "b":
            continue
        if cond == "a" and str(payload.get("condition", "a")).lower() == "b":
            continue
        return p
    return None


def read_telemetry(path: Path) -> tuple[float | None, int | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        tel = payload.get("telemetry") or {}
        tm = payload.get("token_metrics") or {}
        wall = float(tel.get("wall_time_seconds", 0.0) or 0.0)
        comp = int(tm.get("completion_tokens", 0) or 0)
        return (wall if wall > 0 else None, comp if comp > 0 else None)
    except Exception:
        return None, None


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    force = "--force" in argv
    data = MasterLeaderboard.load_data()
    filled = fixed = skipped = 0
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        for slot_key, slot in (entry.get("tasks") or {}).items():
            if not isinstance(slot, dict):
                continue
            cond = str(slot.get("condition") or "a")
            suite = str(slot.get("suite") or "")
            tid = str(slot.get("task_id") or "")

            # runs[] entries carry their own run_dir; backfill each from it.
            runs = slot.get("runs") or []
            targets = [(r, str(r.get("run_dir") or "")) for r in runs if isinstance(r, dict)]
            if not targets and slot.get("run_dir"):
                targets = [(slot, str(slot.get("run_dir")))]
            if not targets:
                skipped += 1
                continue
            touched = False
            for target, rd in targets:
                p = resolve_eval(rd, tid, cond, suite)
                if p is None:
                    continue
                wall, comp = read_telemetry(p)
                has_wall = float(target.get("wall_seconds", 0.0) or 0.0) > 0
                has_comp = int(target.get("completion_tokens", 0) or 0) > 0
                if wall is not None and (force or not has_wall):
                    if has_wall and float(target["wall_seconds"]) != wall:
                        fixed += 1
                    target["wall_seconds"] = round(wall, 1)
                    touched = True
                if comp is not None and (force or not has_comp):
                    if has_comp and int(target["completion_tokens"]) != comp:
                        fixed += 1
                    target["completion_tokens"] = comp
                    touched = True
            if touched:
                filled += 1
            elif not any(resolve_eval(rd, tid, cond, suite) for _, rd in targets):
                skipped += 1
                print(f"  SKIP {key} :: {slot_key} (no matching telemetry file)")
    print(f"backfill: {filled} slots touched, {fixed} values corrected, {skipped} skipped")
    if dry or not filled:
        return 0
    MasterLeaderboard.refresh_aggregates(data)
    MasterLeaderboard.save_data(data)
    MasterLeaderboard.export_markdown(data)
    print("leaderboard.json + LEADERBOARD.md recomputed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
