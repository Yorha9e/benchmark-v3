#!/usr/bin/env python3
"""Backfill per-slot ``wall_seconds`` from each run dir's evaluation.json.

The board only ever stored the LAST run's wall time at entry level, which
made any TPS math meaningless (entry tokens span several run dirs). Every
task's ``evaluation.json`` carries ``telemetry.wall_time_seconds`` — this
script copies it onto the slot (and its runs[] entry) so the board can
aggregate real time, TPS, and cost ratios. 0 LLM tokens, read-only on the
run dirs; the only write is leaderboard.json + LEADERBOARD.md.

Usage::

    python backfill_wall_seconds.py            # backfill + recompute
    python backfill_wall_seconds.py --dry-run  # report coverage only
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "D:/vscode/kimisubagentexplore/subagentbenchmark")

from benchmark_v3.bench_harness.core.report import MasterLeaderboard

LB = Path("bench_runs/leaderboard.json")


def resolve_eval(slot: dict) -> Path | None:
    """Locate evaluation.json for a slot, tolerating suite-dir variations."""
    rd = str(slot.get("run_dir") or "")
    tid = str(slot.get("task_id") or "")
    if not rd or not tid:
        return None
    base = Path(rd)
    suite = str(slot.get("suite") or "")
    for cand in (base / suite / tid,
                 base / f"{suite}_b" / tid,
                 base / "long_b" / tid,
                 base / "short_b" / tid):
        p = cand / "evaluation.json"
        if p.is_file():
            return p
    return None


def read_wall(path: Path) -> float | None:
    try:
        tel = json.loads(path.read_text(encoding="utf-8")).get("telemetry") or {}
        v = float(tel.get("wall_time_seconds", 0.0) or 0.0)
        return v if v > 0 else None
    except Exception:
        return None


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    data = MasterLeaderboard.load_data()
    filled = skipped = 0
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        for slot_key, slot in (entry.get("tasks") or {}).items():
            if not isinstance(slot, dict):
                continue
            if float(slot.get("wall_seconds", 0.0) or 0.0) > 0:
                continue
            p = resolve_eval(slot)
            wall = read_wall(p) if p else None
            if wall is None:
                skipped += 1
                print(f"  SKIP {key} :: {slot_key} (no telemetry)")
                continue
            slot["wall_seconds"] = round(wall, 1)
            for r in slot.get("runs") or []:
                if isinstance(r, dict) and not r.get("wall_seconds"):
                    r["wall_seconds"] = round(wall, 1)
            filled += 1
    print(f"backfill: {filled} slots filled, {skipped} skipped")
    if dry or not filled:
        return 0
    MasterLeaderboard.refresh_aggregates(data)
    MasterLeaderboard.save_data(data)
    MasterLeaderboard.export_markdown(data)
    print("leaderboard.json + LEADERBOARD.md recomputed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
