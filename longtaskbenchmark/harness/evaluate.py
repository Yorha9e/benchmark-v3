from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from common import (
    CLOSED_LOOP_ROOT,
    SchemaError,
    atomic_write_json,
    changed_paths,
    isolated_python_env,
    load_criteria,
    load_manifest,
    tree_snapshot,
)
from instruction_audit import audit_workspace


DEFAULT_CRITERION_TIMEOUT_SECONDS = 60
DIAGNOSTIC_LIMIT = 12000


def _copy_evaluator_bundle(root: Path, bundle_root: Path) -> None:
    package = bundle_root / "closed_loop_v2"
    package.mkdir(parents=True)
    source_init = root / "__init__.py"
    source_evaluator = root / "evaluator"
    if not source_init.is_file() or not source_evaluator.is_dir():
        raise FileNotFoundError("closed-loop evaluator package is incomplete")
    shutil.copy2(source_init, package / "__init__.py")
    shutil.copytree(
        source_evaluator,
        package / "evaluator",
        ignore=shutil.ignore_patterns("criteria.json", "__pycache__", "*.pyc"),
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> str:
    diagnostics: list[str] = []
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0:
                diagnostics.append(
                    "taskkill failed: " + (completed.stderr or completed.stdout or str(completed.returncode)).strip()
                )
                if process.poll() is None:
                    process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as error:
        diagnostics.append(f"process-tree cleanup failed: {type(error).__name__}: {error}")
        try:
            process.kill()
        except OSError as fallback_error:
            diagnostics.append(f"direct kill failed: {type(fallback_error).__name__}: {fallback_error}")
    try:
        process.communicate(timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        diagnostics.append(f"process reap failed: {type(error).__name__}: {error}")
    return "; ".join(diagnostics) if diagnostics else "process tree terminated"


def run_criterion(
    workspace: Path,
    test_name: str,
    *,
    timeout_seconds: int = DEFAULT_CRITERION_TIMEOUT_SECONDS,
    root: Path = CLOSED_LOOP_ROOT,
) -> dict[str, Any]:
    if not test_name.startswith("closed_loop_v2.evaluator."):
        raise SchemaError(f"criterion unittest path is outside evaluator package: {test_name!r}")
    started = time.perf_counter()
    command = [sys.executable, "-m", "unittest", test_name, "-v"]
    try:
        with tempfile.TemporaryDirectory(prefix="criterion-bundle-") as temporary:
            bundle_root = Path(temporary).resolve()
            _copy_evaluator_bundle(root, bundle_root)
            popen_options: dict[str, Any] = {}
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True
            process = subprocess.Popen(
                command,
                cwd=bundle_root,
                env=isolated_python_env((workspace, bundle_root)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_options,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                cleanup = _terminate_process_tree(process)
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                return {
                    "status": "infrastructure_indeterminate",
                    "passed": None,
                    "returncode": None,
                    "duration_ms": duration_ms,
                    "diagnostics": (
                        f"criterion subprocess timeout after {timeout_seconds} seconds; {cleanup}; "
                        "timeout/cleanup is infrastructure safety evidence, not candidate failure"
                    ),
                    "command": command,
                }
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            diagnostics = (stdout + stderr)[-DIAGNOSTIC_LIMIT:]
            status = "passed" if process.returncode == 0 else "failed"
            return {
                "status": status,
                "passed": status == "passed",
                "returncode": process.returncode,
                "duration_ms": duration_ms,
                "diagnostics": diagnostics,
                "command": command,
            }
    except OSError as error:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            "status": "infrastructure_indeterminate",
            "passed": None,
            "returncode": None,
            "duration_ms": duration_ms,
            "diagnostics": f"cannot prepare/start criterion subprocess: {type(error).__name__}: {error}",
            "command": command,
        }


def evaluate(
    workspace: Path,
    slot: str,
    *,
    root: Path = CLOSED_LOOP_ROOT,
    timeout_seconds: int = DEFAULT_CRITERION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    manifest = load_manifest(root)
    if slot not in manifest["candidate_slots"]:
        raise SchemaError(f"unknown slot: {slot!r}")
    criteria_manifest = load_criteria(root, manifest)
    workspace = workspace.resolve()
    before = tree_snapshot(workspace)
    instruction = audit_workspace(workspace, slot, root)
    gate = instruction["instruction_gate"]

    projects: list[dict[str, Any]] = []
    passed_criteria = 0
    strict_milestone_count = 0
    infrastructure_indeterminate: list[str] = []
    for project in criteria_manifest["projects"]:
        milestones: list[dict[str, Any]] = []
        for milestone in project["milestones"]:
            criterion_results: list[dict[str, Any]] = []
            for criterion in milestone["criteria"]:
                result = run_criterion(
                    workspace,
                    criterion["unittest"],
                    timeout_seconds=timeout_seconds,
                    root=root,
                )
                result.update(
                    {
                        "id": criterion["id"],
                        "capability_id": criterion["capability_id"],
                        "unittest": criterion["unittest"],
                    }
                )
                criterion_results.append(result)
                if result["status"] == "passed":
                    passed_criteria += 1
                elif result["status"] == "infrastructure_indeterminate":
                    infrastructure_indeterminate.append(criterion["id"])
            strict_success = gate is True and all(item["status"] == "passed" for item in criterion_results)
            if strict_success:
                strict_milestone_count += 1
            milestones.append(
                {
                    "id": milestone["id"],
                    "capability_id": milestone["capability_id"],
                    "strict_success": strict_success,
                    "criteria": criterion_results,
                }
            )
        projects.append(
            {
                "id": project["id"],
                "closed_loop_success": all(milestone["strict_success"] for milestone in milestones),
                "milestones": milestones,
            }
        )

    after = tree_snapshot(workspace)
    changed = changed_paths(before["files"], after["files"])
    if changed:
        if gate is not None:
            gate = False
            instruction["instruction_gate"] = False
        instruction["checks"].append(
            {
                "id": "workspace_unchanged_during_evaluation",
                "observable": True,
                "status": "failed",
                "passed": False,
                "diagnostics": changed,
            }
        )
        strict_milestone_count = 0
        for project in projects:
            project["closed_loop_success"] = False
            for milestone in project["milestones"]:
                milestone["strict_success"] = False
    if instruction["decision_infrastructure_indeterminate"]:
        infrastructure_indeterminate.append("instruction_audit")
    criterion_total = criteria_manifest["criterion_count"]
    return {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "slot": slot,
        "model": manifest["candidate_slots"][slot]["model"],
        "workspace": workspace.as_posix(),
        "instruction": instruction,
        "InstructionGate": gate,
        "ClosedLoopProjectCount": sum(project["closed_loop_success"] for project in projects),
        "MilestoneStrictCount": strict_milestone_count,
        "AcceptanceCoverage": passed_criteria / criterion_total,
        "criterion_pass_count": passed_criteria,
        "criterion_count": criterion_total,
        "decision_infrastructure_indeterminate": bool(infrastructure_indeterminate),
        "infrastructure_indeterminate_evidence": sorted(set(infrastructure_indeterminate)),
        "projects": projects,
        "workspace_before": before,
        "workspace_after": after,
        "workspace_changed_paths": changed,
        "workspace_unchanged_during_evaluation": not changed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate one closed-loop v2 workspace.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--criterion-timeout", type=int, default=DEFAULT_CRITERION_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.criterion_timeout <= 0:
        raise SystemExit("--criterion-timeout must be positive")
    payload = evaluate(args.workspace, args.slot, timeout_seconds=args.criterion_timeout)
    if args.output:
        atomic_write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
