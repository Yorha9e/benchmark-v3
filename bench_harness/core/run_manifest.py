"""L1 run-level pause / continue: manifest + pause sentinel between tasks.

Pause is cooperative. Operators may:

* create ``PAUSE.request`` in the run dir, or
* press Ctrl+C once

If a task is mid-flight, it is **abandoned** (no ``evaluation.json``, snapshot
cleared) and the run freezes with prior completed tasks kept. The abandoned
task stays in ``remaining`` and will be retried on continue.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.snapshot import SnapshotManager, atomic_write_json

__all__ = [
    "MANIFEST_NAME",
    "PAUSE_SENTINEL_NAME",
    "INCOMPLETE_STATUSES",
    "TaskAbandoned",
    "build_planned_queue",
    "task_eval_path",
    "task_is_complete",
    "load_manifest",
    "save_manifest",
    "new_manifest",
    "mark_task_completed",
    "set_manifest_status",
    "pause_requested",
    "clear_pause_request",
    "request_pause",
    "abandon_incomplete_task",
    "run_dir_from_task_paths",
    "list_incomplete_runs",
    "manifest_to_launch_config",
]

MANIFEST_NAME = "run_manifest.json"
PAUSE_SENTINEL_NAME = "PAUSE.request"
INCOMPLETE_STATUSES = frozenset({"running", "paused", "interrupted"})


class TaskAbandoned(Exception):
    """Current task aborted by L1 pause; do not persist a scored evaluation."""

    def __init__(self, reason: str = "pause") -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_dir_from_task_paths(task_root: Path) -> Path:
    """``bench_runs/<stamp>/<suite>/<task>`` → ``bench_runs/<stamp>``."""
    return Path(task_root).resolve().parent.parent


def abandon_incomplete_task(output_dir: Path, suite: str, task_id: str) -> None:
    """Drop partial score artifacts so the task stays retryable on continue."""
    root = Path(output_dir) / suite / task_id
    for name in ("evaluation.json", "summary.json"):
        path = root / name
        try:
            path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if path.exists():
                path.unlink()
        except OSError:
            pass
    try:
        SnapshotManager(root).clear()
    except Exception:
        pass


def build_planned_queue(
    suite_names: list[str],
    *,
    task_filter: str | None = None,
) -> list[dict[str, str]]:
    """Expand suite keys into ordered ``{suite, task_id}`` entries."""
    from benchmark_v3.bench_harness.suites import get_suite

    planned: list[dict[str, str]] = []
    for suite_name in suite_names:
        suite = get_suite(suite_name)
        for task_id in suite.task_ids():
            if task_filter and task_id != task_filter:
                continue
            planned.append({"suite": suite_name, "task_id": task_id})
    return planned


def task_eval_path(output_dir: Path, suite: str, task_id: str) -> Path:
    return Path(output_dir) / suite / task_id / "evaluation.json"


def task_is_complete(output_dir: Path, suite: str, task_id: str) -> bool:
    return task_eval_path(output_dir, suite, task_id).is_file()


def load_manifest(output_dir: Path) -> dict[str, Any] | None:
    path = Path(output_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def save_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    manifest = dict(manifest)
    manifest["updated_at"] = _utc_now()
    target = Path(output_dir) / MANIFEST_NAME
    atomic_write_json(target, manifest)
    return target


def new_manifest(
    *,
    output_dir: Path,
    planned: list[dict[str, str]],
    launch: dict[str, Any],
) -> dict[str, Any]:
    """Create a fresh running manifest; remaining = not-yet-evaluated tasks."""
    completed: list[dict[str, Any]] = []
    remaining: list[dict[str, str]] = []
    for item in planned:
        suite = item["suite"]
        task_id = item["task_id"]
        if task_is_complete(output_dir, suite, task_id):
            completed.append({"suite": suite, "task_id": task_id, "skipped": True})
        else:
            remaining.append({"suite": suite, "task_id": task_id})
    manifest: dict[str, Any] = {
        "version": 1,
        "status": "running",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "output_dir": str(Path(output_dir)),
        "planned": list(planned),
        "completed": completed,
        "remaining": remaining,
        "pause_reason": "",
        "launch": dict(launch),
    }
    save_manifest(output_dir, manifest)
    return manifest


def mark_task_completed(
    manifest: dict[str, Any],
    *,
    suite: str,
    task_id: str,
    passed: bool | None = None,
    reward: float | None = None,
) -> None:
    remaining = [
        item
        for item in (manifest.get("remaining") or [])
        if not (item.get("suite") == suite and item.get("task_id") == task_id)
    ]
    manifest["remaining"] = remaining
    completed = list(manifest.get("completed") or [])
    entry: dict[str, Any] = {"suite": suite, "task_id": task_id}
    if passed is not None:
        entry["passed"] = bool(passed)
    if reward is not None:
        entry["reward"] = reward
    # replace prior skip placeholder for same task
    completed = [
        c
        for c in completed
        if not (c.get("suite") == suite and c.get("task_id") == task_id)
    ]
    completed.append(entry)
    manifest["completed"] = completed


def set_manifest_status(
    output_dir: Path,
    manifest: dict[str, Any],
    status: str,
    *,
    reason: str = "",
) -> None:
    manifest["status"] = status
    if reason:
        manifest["pause_reason"] = reason
    if status == "paused":
        manifest["paused_at"] = _utc_now()
    elif status == "completed":
        manifest["completed_at"] = _utc_now()
        manifest["remaining"] = []
    save_manifest(output_dir, manifest)


def pause_sentinel_path(output_dir: Path) -> Path:
    return Path(output_dir) / PAUSE_SENTINEL_NAME


def pause_requested(output_dir: Path, flag: Any | None = None) -> bool:
    """True if pause sentinel exists or *flag* is truthy / ``Event.is_set()``."""
    if pause_sentinel_path(output_dir).is_file():
        return True
    if flag is None:
        return False
    try:
        if hasattr(flag, "is_set"):
            return bool(flag.is_set())
        if isinstance(flag, dict):
            return bool(flag.get("armed"))
        return bool(flag)
    except Exception:
        return False


def clear_pause_request(output_dir: Path) -> None:
    path = pause_sentinel_path(output_dir)
    try:
        path.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def request_pause(output_dir: Path) -> Path:
    path = pause_sentinel_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pause\n", encoding="utf-8")
    return path


def list_incomplete_runs(runs_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Scan ``bench_runs/*/run_manifest.json`` for resumable runs."""
    root = Path(runs_root or "bench_runs")
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        manifest = load_manifest(child)
        if not manifest:
            continue
        status = str(manifest.get("status") or "")
        remaining = list(manifest.get("remaining") or [])
        if status not in INCOMPLETE_STATUSES:
            continue
        if status == "completed" or not remaining:
            # running with empty remaining == done but status stale
            if not remaining and status == "running":
                continue
            if not remaining:
                continue
        launch = manifest.get("launch") or {}
        found.append(
            {
                "run_dir": str(child),
                "status": status,
                "model_id": launch.get("model") or launch.get("model_id") or "",
                "driver": launch.get("driver") or "",
                "effort": launch.get("effort"),
                "completed_n": len(manifest.get("completed") or []),
                "remaining_n": len(remaining),
                "planned_n": len(manifest.get("planned") or []),
                "updated_at": manifest.get("updated_at") or "",
                "pause_reason": manifest.get("pause_reason") or "",
                "manifest": manifest,
            }
        )
    return found


def discard_run(output_dir: str | Path) -> bool:
    """Mark a run discarded so it vanishes from the continue picker.

    Files on disk are kept (forensics stay possible); only the manifest
    status flips to ``discarded``, which is outside INCOMPLETE_STATUSES.
    Returns False when no manifest exists.
    """
    manifest = load_manifest(Path(output_dir))
    if not manifest:
        return False
    manifest["status"] = "discarded"
    manifest["pause_reason"] = "discarded by user"
    save_manifest(Path(output_dir), manifest)
    return True


def manifest_to_launch_config(manifest: dict[str, Any], run_dir: str | Path) -> dict[str, Any]:
    """Rebuild a TUI/CLI launch config from a saved manifest."""
    launch = dict(manifest.get("launch") or {})
    config: dict[str, Any] = {
        "driver": launch.get("driver") or "openai",
        "model": launch.get("model") or launch.get("model_id") or "",
        "base_url": launch.get("base_url") or "",
        "api_key": launch.get("api_key") or "",
        "proxy": launch.get("proxy") or "",
        "effort": launch.get("effort"),
        "suites": list(launch.get("suites") or []),
        "resume": True,
        "export_sft": launch.get("export_sft") or "",
        "export_dpo": launch.get("export_dpo") or "",
        "judge_model": launch.get("judge_model") or "",
        "judge_driver": launch.get("judge_driver") or "",
        "judge_base_url": launch.get("judge_base_url") or "",
        "judge_api_key": launch.get("judge_api_key") or "",
        "judge_effort": launch.get("judge_effort"),
        "output": str(run_dir),
        "continue_run": True,
        "on_regress": launch.get("on_regress") or "ask",
    }
    if not config["suites"]:
        # recover suite list from planned queue
        seen: list[str] = []
        for item in manifest.get("planned") or []:
            s = str(item.get("suite") or "")
            if s and s not in seen:
                seen.append(s)
        config["suites"] = seen
    return config


def self_test() -> tuple[int, int]:
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        print(f"{'PASS' if cond else 'FAIL'} run_manifest::{name}", flush=True)
        counts[0 if cond else 1] += 1

    with tempfile.TemporaryDirectory(prefix="run-manifest-") as tmp:
        root = Path(tmp)
        planned = [
            {"suite": "short", "task_id": "varint_parser"},
            {"suite": "short", "task_id": "timing_wheel"},
        ]
        launch = {"model": "m", "driver": "mock", "suites": ["short"]}
        # pretend first task already done
        done = root / "short" / "varint_parser"
        done.mkdir(parents=True)
        (done / "evaluation.json").write_text("{}", encoding="utf-8")
        man = new_manifest(output_dir=root, planned=planned, launch=launch)
        check("skips_complete", len(man["remaining"]) == 1)
        check("completed_placeholder", len(man["completed"]) == 1)
        mark_task_completed(man, suite="short", task_id="timing_wheel", passed=True, reward=1.0)
        set_manifest_status(root, man, "paused", reason="test")
        loaded = load_manifest(root)
        check("roundtrip", loaded is not None and loaded.get("status") == "paused")
        request_pause(root)
        check("pause_sentinel", pause_requested(root))
        clear_pause_request(root)
        check("pause_cleared", not pause_requested(root))
        cfg = manifest_to_launch_config(loaded or man, root)
        check("launch_config_model", cfg.get("model") == "m")
        check("launch_config_output", cfg.get("output") == str(root))
        check("continue_flag", cfg.get("continue_run") is True)

        # abandon scrub
        tdir = root / "short" / "timing_wheel"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "evaluation.json").write_text("{}", encoding="utf-8")
        (tdir / "summary.json").write_text("{}", encoding="utf-8")
        abandon_incomplete_task(root, "short", "timing_wheel")
        check("abandon_drops_eval", not (tdir / "evaluation.json").exists())
        check("abandon_drops_summary", not (tdir / "summary.json").exists())

        # list_incomplete_runs: point a fake bench_runs child
        runs = root / "bench_runs" / "fake-run"
        runs.mkdir(parents=True)
        save_manifest(
            runs,
            {
                "version": 1,
                "status": "paused",
                "remaining": [{"suite": "short", "task_id": "timing_wheel"}],
                "completed": [],
                "planned": planned,
                "launch": launch,
            },
        )
        listed = list_incomplete_runs(root / "bench_runs")
        check("list_incomplete", len(listed) == 1 and listed[0]["remaining_n"] == 1)
        check("discard_hides", discard_run(runs) is True)
        check("discard_keeps_files", (runs / MANIFEST_NAME).is_file())
        check("discard_gone_from_list", list_incomplete_runs(root / "bench_runs") == [])
        check("discard_missing", discard_run(root / "bench_runs" / "nope") is False)

    return counts[0], counts[1]


if __name__ == "__main__":
    p, f = self_test()
    print(f"run_manifest self-test: {p} passed, {f} failed")
    raise SystemExit(f)
