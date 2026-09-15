from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    CLOSED_LOOP_ROOT,
    atomic_write_json,
    atomic_write_jsonl,
    load_criteria,
    load_manifest,
    ordered_slots,
    tree_snapshot,
    utc_now,
)
from evaluate import DEFAULT_CRITERION_TIMEOUT_SECONDS, evaluate


def _indeterminate_record(
    root: Path,
    manifest: dict[str, Any],
    criteria_manifest: dict[str, Any],
    slot: str,
    error: BaseException,
) -> dict[str, Any]:
    workspace = (root / "runs" / slot / "workspace").resolve()
    snapshot = tree_snapshot(workspace)
    diagnostic = f"evaluation command error: {type(error).__name__}: {error}"
    projects = []
    for expected_project in criteria_manifest["projects"]:
        milestones = []
        for expected_milestone in expected_project["milestones"]:
            criterion_results = [
                {
                    "id": criterion["id"],
                    "capability_id": criterion["capability_id"],
                    "unittest": criterion["unittest"],
                    "status": "infrastructure_indeterminate",
                    "passed": None,
                    "returncode": None,
                    "duration_ms": 0.0,
                    "diagnostics": diagnostic,
                    "command": [],
                }
                for criterion in expected_milestone["criteria"]
            ]
            milestones.append(
                {
                    "id": expected_milestone["id"],
                    "capability_id": expected_milestone["capability_id"],
                    "strict_success": False,
                    "criteria": criterion_results,
                }
            )
        projects.append(
            {"id": expected_project["id"], "closed_loop_success": False, "milestones": milestones}
        )
    return {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "evaluated_at": utc_now(),
        "slot": slot,
        "model": manifest["candidate_slots"][slot]["model"],
        "workspace": workspace.as_posix(),
        "instruction": {
            "schema_version": manifest["schema_version"],
            "benchmark_version": manifest["benchmark_version"],
            "slot": slot,
            "workspace": workspace.as_posix(),
            "checks": [],
            "instruction_gate": None,
            "decision_infrastructure_indeterminate": True,
        },
        "InstructionGate": None,
        "ClosedLoopProjectCount": 0,
        "MilestoneStrictCount": 0,
        "AcceptanceCoverage": 0.0,
        "criterion_pass_count": 0,
        "criterion_count": criteria_manifest["criterion_count"],
        "decision_infrastructure_indeterminate": True,
        "infrastructure_indeterminate_evidence": [
            diagnostic,
            *[
                criterion["id"]
                for project in criteria_manifest["projects"]
                for milestone in project["milestones"]
                for criterion in milestone["criteria"]
            ],
        ],
        "projects": projects,
        "workspace_before": snapshot,
        "workspace_after": snapshot,
        "workspace_changed_paths": [],
        "workspace_unchanged_during_evaluation": True,
    }


def evaluate_all(
    *,
    root: Path = CLOSED_LOOP_ROOT,
    output: Path | None = None,
    audit_output: Path | None = None,
    timeout_seconds: int = DEFAULT_CRITERION_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    manifest = load_manifest(root)
    criteria_manifest = load_criteria(root, manifest)
    records: list[dict[str, Any]] = []
    for slot in ordered_slots(manifest):
        workspace = root / "runs" / slot / "workspace"
        try:
            records.append(evaluate(workspace, slot, root=root, timeout_seconds=timeout_seconds))
        except BaseException as error:
            records.append(_indeterminate_record(root, manifest, criteria_manifest, slot, error))

    actual_output = output or root / "results" / "evaluation.jsonl"
    atomic_write_jsonl(actual_output, records)
    audit = {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": utc_now(),
        "record_count": len(records),
        "slots": [record["slot"] for record in records],
        "publication_blocked": any(record["decision_infrastructure_indeterminate"] for record in records),
        "workspace_trees": {
            record["slot"]: {
                "before": record["workspace_before"],
                "after": record["workspace_after"],
                "changed_paths": record["workspace_changed_paths"],
            }
            for record in records
        },
    }
    atomic_write_json(audit_output or root / "results" / "evaluation-audit.json", audit)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate every manifest slot exactly once.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--criterion-timeout", type=int, default=DEFAULT_CRITERION_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.criterion_timeout <= 0:
        raise SystemExit("--criterion-timeout must be positive")
    records = evaluate_all(
        output=args.output,
        audit_output=args.audit_output,
        timeout_seconds=args.criterion_timeout,
    )
    print(
        json.dumps(
            {
                "evaluation_records": len(records),
                "publication_blocked": any(record["decision_infrastructure_indeterminate"] for record in records),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
