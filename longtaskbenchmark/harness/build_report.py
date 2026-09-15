from __future__ import annotations

import argparse
import csv
import io
import json
import math
from pathlib import Path
from typing import Any, Mapping

from common import (
    CLOSED_LOOP_ROOT,
    PROJECT_ROOT,
    RANKING_AXES,
    SchemaError,
    atomic_write_bytes,
    atomic_write_json,
    changed_paths,
    expected_model,
    finite_nonnegative_integer,
    load_criteria,
    load_json,
    load_jsonl,
    load_manifest,
    ordered_slots,
    sha256_file,
    tree_hashes,
    tree_snapshot,
    utc_now,
    validate_slot_model,
    validate_tree_snapshot,
    verify_assets,
)


LEADERBOARD_COLUMNS = (
    "Rank",
    "RankStatus",
    "Slot",
    "Model",
    "InstructionGate",
    "ClosedLoopProjectCount",
    "MilestoneStrictCount",
    "AcceptanceCoverage",
    "InferenceTokens",
    "InferenceTokensPerMilestoneStrictSuccess",
    "TokenMeasurementStatus",
    "CandidateTokenSoftSLA",
    "DecisionInfrastructureIndeterminate",
    "DecisionFinalist",
)
INSTRUCTION_CHECKS = (
    ("frozen_assets", True),
    ("workspace_exists", True),
    ("workspace_non_source", True),
    ("allowed_packages_parse", True),
    ("public_exports_and_signatures", True),
    ("standard_library_only_imports", True),
    ("no_forbidden_network_process_or_dynamic_code", True),
    ("process_no_delegation_attempt", False),
    ("process_no_hidden_evaluator_read_attempt", False),
    ("process_no_network_attempt", False),
    ("writes_only_declared_storage", False),
)


def _strict_instruction(
    instruction: Any,
    record: Mapping[str, Any],
    gate: bool | None,
    slot: str,
) -> tuple[bool, bool]:
    if not isinstance(instruction, dict) or set(instruction) != {
        "schema_version",
        "benchmark_version",
        "slot",
        "workspace",
        "checks",
        "instruction_gate",
        "decision_infrastructure_indeterminate",
    }:
        raise SchemaError(f"evaluation {slot} instruction schema mismatch")
    if (
        instruction["schema_version"] != record["schema_version"]
        or instruction["benchmark_version"] != record["benchmark_version"]
        or instruction["slot"] != slot
        or instruction["workspace"] != record["workspace"]
    ):
        raise SchemaError(f"evaluation {slot} instruction identity mismatch")
    checks = instruction["checks"]
    if not isinstance(checks, list):
        raise SchemaError(f"evaluation {slot} instruction checks must be a list")
    if not checks:
        if gate is not None or instruction["instruction_gate"] is not None:
            raise SchemaError(f"evaluation {slot} empty instruction checks cannot establish a gate")
        if instruction["decision_infrastructure_indeterminate"] is not True:
            raise SchemaError(f"evaluation {slot} empty instruction checks must be infrastructure indeterminate")
        return True, False

    expected_checks = list(INSTRUCTION_CHECKS)
    if record["workspace_changed_paths"]:
        expected_checks.append(("workspace_unchanged_during_evaluation", True))
    if len(checks) != len(expected_checks):
        raise SchemaError(f"evaluation {slot} instruction check set is incomplete")
    observable_statuses: list[str] = []
    check_ids: list[str] = []
    for index, (check, (expected_id, expected_observable)) in enumerate(
        zip(checks, expected_checks)
    ):
        if not isinstance(check, dict) or set(check) != {
            "id",
            "observable",
            "status",
            "passed",
            "diagnostics",
        }:
            raise SchemaError(f"evaluation {slot} instruction check[{index}] schema mismatch")
        check_id = check["id"]
        observable = check["observable"]
        status = check["status"]
        expected_passed = {
            "passed": True,
            "failed": False,
            "unobservable": None,
            "infrastructure_indeterminate": None,
        }.get(status, "invalid")
        if (
            check_id != expected_id
            or observable is not expected_observable
            or expected_passed == "invalid"
            or check["passed"] is not expected_passed
        ):
            raise SchemaError(f"evaluation {slot} instruction check[{index}] is invalid")
        if observable and status == "unobservable":
            raise SchemaError(f"evaluation {slot} observable instruction check is unobservable")
        if not observable and status != "unobservable":
            raise SchemaError(f"evaluation {slot} non-observable instruction check claims a result")
        if observable:
            observable_statuses.append(status)
        check_ids.append(check_id)
    if len(check_ids) != len(set(check_ids)):
        raise SchemaError(f"evaluation {slot} instruction check ids are not unique")
    if not observable_statuses:
        raise SchemaError(f"evaluation {slot} instruction has no observable checks")
    derived_gate: bool | None
    if "infrastructure_indeterminate" in observable_statuses:
        derived_gate = None
    else:
        derived_gate = all(status == "passed" for status in observable_statuses)
    if instruction["instruction_gate"] is not derived_gate or gate is not derived_gate:
        raise SchemaError(f"evaluation {slot} InstructionGate does not match observable checks")
    instruction_indeterminate = instruction["decision_infrastructure_indeterminate"]
    if instruction_indeterminate is not (derived_gate is None):
        raise SchemaError(f"evaluation {slot} instruction infrastructure flag is inconsistent")
    return instruction_indeterminate, True


def _strict_nested_projects(
    projects: Any,
    expected_projects: list[dict[str, Any]],
    gate: bool | None,
    slot: str,
) -> tuple[int, int, int, list[str], list[str]]:
    if not isinstance(projects, list) or len(projects) != len(expected_projects):
        raise SchemaError(f"evaluation {slot} projects do not match criteria manifest")
    project_successes = 0
    milestone_successes = 0
    criterion_passes = 0
    statuses: list[str] = []
    indeterminate_ids: list[str] = []
    criterion_keys = {
        "id",
        "capability_id",
        "unittest",
        "status",
        "passed",
        "returncode",
        "duration_ms",
        "diagnostics",
        "command",
    }
    for project_index, (project, expected_project) in enumerate(zip(projects, expected_projects)):
        if not isinstance(project, dict) or set(project) != {"id", "closed_loop_success", "milestones"}:
            raise SchemaError(f"evaluation {slot} project[{project_index}] schema mismatch")
        if project["id"] != expected_project["id"]:
            raise SchemaError(f"evaluation {slot} project order/id mismatch")
        expected_milestones = expected_project["milestones"]
        milestones = project["milestones"]
        if not isinstance(milestones, list) or len(milestones) != len(expected_milestones):
            raise SchemaError(f"evaluation {slot} project {project['id']} milestones mismatch")
        derived_project = True
        for milestone_index, (milestone, expected_milestone) in enumerate(
            zip(milestones, expected_milestones)
        ):
            if not isinstance(milestone, dict) or set(milestone) != {
                "id",
                "capability_id",
                "strict_success",
                "criteria",
            }:
                raise SchemaError(
                    f"evaluation {slot} milestone[{project_index}][{milestone_index}] schema mismatch"
                )
            if (
                milestone["id"] != expected_milestone["id"]
                or milestone["capability_id"] != expected_milestone["capability_id"]
            ):
                raise SchemaError(f"evaluation {slot} milestone id/capability/order mismatch")
            expected_criteria = expected_milestone["criteria"]
            criteria = milestone["criteria"]
            if len(expected_criteria) != 2:
                raise SchemaError(f"criteria manifest milestone {expected_milestone['id']} must contain two criteria")
            if not isinstance(criteria, list) or len(criteria) != len(expected_criteria):
                raise SchemaError(f"evaluation {slot} milestone {milestone['id']} criteria mismatch")
            milestone_statuses: list[str] = []
            for criterion_index, (criterion, expected_criterion) in enumerate(
                zip(criteria, expected_criteria)
            ):
                if not isinstance(criterion, dict) or set(criterion) != criterion_keys:
                    raise SchemaError(
                        f"evaluation {slot} criterion[{project_index}][{milestone_index}][{criterion_index}] schema mismatch"
                    )
                for field in ("id", "capability_id", "unittest"):
                    if criterion[field] != expected_criterion[field]:
                        raise SchemaError(f"evaluation {slot} criterion {field}/order mismatch")
                status = criterion["status"]
                expected_passed = {
                    "passed": True,
                    "failed": False,
                    "infrastructure_indeterminate": None,
                }.get(status, "invalid")
                if expected_passed == "invalid" or criterion["passed"] is not expected_passed:
                    raise SchemaError(f"evaluation {slot} criterion status/passed mismatch")
                if not isinstance(criterion["diagnostics"], str):
                    raise SchemaError(f"evaluation {slot} criterion diagnostics must be text")
                if not isinstance(criterion["command"], list) or any(
                    not isinstance(item, str) for item in criterion["command"]
                ):
                    raise SchemaError(f"evaluation {slot} criterion command is invalid")
                duration = criterion["duration_ms"]
                if (
                    isinstance(duration, bool)
                    or not isinstance(duration, (int, float))
                    or not math.isfinite(duration)
                    or duration < 0
                ):
                    raise SchemaError(f"evaluation {slot} criterion duration is invalid")
                returncode = criterion["returncode"]
                if status == "infrastructure_indeterminate":
                    if returncode is not None:
                        raise SchemaError(f"evaluation {slot} indeterminate criterion has returncode")
                elif isinstance(returncode, bool) or not isinstance(returncode, int):
                    raise SchemaError(f"evaluation {slot} completed criterion lacks returncode")
                statuses.append(status)
                if status == "infrastructure_indeterminate":
                    indeterminate_ids.append(criterion["id"])
                milestone_statuses.append(status)
                criterion_passes += int(status == "passed")
            derived_milestone = gate is True and all(
                status == "passed" for status in milestone_statuses
            )
            if milestone["strict_success"] is not derived_milestone:
                raise SchemaError(f"evaluation {slot} milestone strict_success is forged")
            milestone_successes += int(derived_milestone)
            derived_project = derived_project and derived_milestone
        if project["closed_loop_success"] is not derived_project:
            raise SchemaError(f"evaluation {slot} project closed_loop_success is forged")
        project_successes += int(derived_project)
    return project_successes, milestone_successes, criterion_passes, statuses, indeterminate_ids


def _strict_evaluations(
    records: list[dict[str, Any]],
    manifest: Mapping[str, Any],
    criteria_manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    by_slot: dict[str, dict[str, Any]] = {}
    criterion_total = criteria_manifest["criterion_count"]
    expected_keys = {
        "schema_version",
        "benchmark_version",
        "evaluated_at",
        "slot",
        "model",
        "workspace",
        "instruction",
        "InstructionGate",
        "ClosedLoopProjectCount",
        "MilestoneStrictCount",
        "AcceptanceCoverage",
        "criterion_pass_count",
        "criterion_count",
        "decision_infrastructure_indeterminate",
        "infrastructure_indeterminate_evidence",
        "projects",
        "workspace_before",
        "workspace_after",
        "workspace_changed_paths",
        "workspace_unchanged_during_evaluation",
    }
    for index, record in enumerate(records):
        missing = sorted(expected_keys - record.keys())
        unknown = sorted(record.keys() - expected_keys)
        if missing or unknown:
            raise SchemaError(f"evaluation[{index}] keys invalid; missing={missing!r}, unknown={unknown!r}")
        if (
            record["schema_version"] != manifest["schema_version"]
            or record["benchmark_version"] != manifest["benchmark_version"]
        ):
            raise SchemaError(f"evaluation[{index}] version mismatch")
        slot = record["slot"]
        validate_slot_model(manifest, slot, record["model"])
        if slot in by_slot:
            raise SchemaError(f"duplicate evaluation slot: {slot}")
        gate = record["InstructionGate"]
        if gate is not True and gate is not False and gate is not None:
            raise SchemaError(f"evaluation {slot} has invalid InstructionGate")
        if record["criterion_count"] != criterion_total:
            raise SchemaError(f"evaluation {slot} criterion_count mismatch")

        instruction_indeterminate, instruction_has_checks = _strict_instruction(
            record["instruction"], record, gate, slot
        )

        (
            project_count,
            milestone_count,
            criterion_passes,
            criterion_statuses,
            indeterminate_criterion_ids,
        ) = _strict_nested_projects(record["projects"], criteria_manifest["projects"], gate, slot)
        recomputed = {
            "ClosedLoopProjectCount": project_count,
            "MilestoneStrictCount": milestone_count,
            "criterion_pass_count": criterion_passes,
        }
        for field, value in recomputed.items():
            if record[field] != value or not finite_nonnegative_integer(record[field]):
                raise SchemaError(f"evaluation {slot} {field} does not match nested evidence")
        coverage = record["AcceptanceCoverage"]
        expected_coverage = criterion_passes / criterion_total
        if (
            isinstance(coverage, bool)
            or not isinstance(coverage, (int, float))
            or not math.isfinite(coverage)
            or not math.isclose(float(coverage), expected_coverage, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise SchemaError(f"evaluation {slot} AcceptanceCoverage does not match nested evidence")
        if gate is not True and (project_count != 0 or milestone_count != 0):
            raise SchemaError(f"evaluation {slot} gate failure must zero strict success counts")

        top_indeterminate = record["decision_infrastructure_indeterminate"]
        if not isinstance(top_indeterminate, bool):
            raise SchemaError(f"evaluation {slot} has invalid infrastructure flag")
        derived_indeterminate = (
            gate is None
            or instruction_indeterminate
            or "infrastructure_indeterminate" in criterion_statuses
        )
        if top_indeterminate is not derived_indeterminate:
            raise SchemaError(f"evaluation {slot} infrastructure flag does not match derived evidence")
        evidence = record["infrastructure_indeterminate_evidence"]
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item for item in evidence
        ):
            raise SchemaError(f"evaluation {slot} has invalid infrastructure evidence")
        if instruction_has_checks:
            expected_evidence = sorted(
                {
                    *indeterminate_criterion_ids,
                    *(["instruction_audit"] if gate is None else []),
                }
            )
            if evidence != expected_evidence:
                raise SchemaError(f"evaluation {slot} infrastructure evidence does not match derived sources")
        else:
            if (
                len(evidence) != len(indeterminate_criterion_ids) + 1
                or not evidence[0].startswith("evaluation command error: ")
                or evidence[1:] != indeterminate_criterion_ids
            ):
                raise SchemaError(f"evaluation {slot} command-error infrastructure evidence is invalid")

        for tree_name in ("workspace_before", "workspace_after"):
            validate_tree_snapshot(record[tree_name], f"evaluation {slot} {tree_name}")
        if not isinstance(record["workspace_changed_paths"], list):
            raise SchemaError(f"evaluation {slot} workspace_changed_paths must be a list")
        recomputed_changes = changed_paths(
            record["workspace_before"]["files"], record["workspace_after"]["files"]
        )
        if record["workspace_changed_paths"] != recomputed_changes:
            raise SchemaError(f"evaluation {slot} workspace changed-path evidence mismatch")
        if record["workspace_unchanged_during_evaluation"] is not (not recomputed_changes):
            raise SchemaError(f"evaluation {slot} workspace unchanged flag mismatch")
        by_slot[slot] = record
    if set(by_slot) != set(manifest["candidate_slots"]):
        raise SchemaError("evaluations must contain every manifest slot exactly once")
    return by_slot


def _strict_usages(records: list[dict[str, Any]], manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    by_slot: dict[str, dict[str, Any]] = {}
    required = {
        "schema_version",
        "role",
        "slot",
        "model",
        "token_measurement_status",
        "token_measurement_valid",
        "inference_tokens",
    }
    for index, record in enumerate(records):
        missing = sorted(required - record.keys())
        if missing:
            raise SchemaError(f"usage[{index}] missing fields: {missing!r}")
        if record["schema_version"] != manifest["schema_version"]:
            raise SchemaError(f"usage[{index}] schema_version mismatch")
        if record["role"] != "executor":
            raise SchemaError("candidate usage input must not contain main-agent/planner records")
        slot = record["slot"]
        validate_slot_model(manifest, slot, record["model"])
        if slot in by_slot:
            raise SchemaError(f"duplicate usage slot: {slot}")
        valid = record["token_measurement_valid"]
        expected_status = "valid" if valid is True else "indeterminate"
        if not isinstance(valid, bool) or record["token_measurement_status"] != expected_status:
            raise SchemaError(f"usage {slot} token status is inconsistent")
        if valid:
            for field in ("inference_tokens", "input_context_tokens", "output_tokens", "fresh_tokens"):
                if not finite_nonnegative_integer(record.get(field)):
                    raise SchemaError(f"usage {slot} has invalid {field}")
        elif record["inference_tokens"] is not None:
            raise SchemaError(f"usage {slot} indeterminate measurement must not expose inference_tokens")
        by_slot[slot] = record
    if set(by_slot) != set(manifest["candidate_slots"]):
        raise SchemaError("usage records must contain every manifest slot exactly once")
    return by_slot


def build_rows(
    manifest: Mapping[str, Any],
    evaluations: list[dict[str, Any]],
    usages: list[dict[str, Any]],
    criteria_manifest: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    actual_criteria = criteria_manifest or load_criteria(CLOSED_LOOP_ROOT, manifest)
    evaluation_by_slot = _strict_evaluations(evaluations, manifest, actual_criteria)
    usage_by_slot = _strict_usages(usages, manifest)
    token_sla = manifest["resource_sla"]["candidate_inference_tokens_soft"]
    rows: list[dict[str, Any]] = []
    for slot in ordered_slots(manifest):
        evaluation = evaluation_by_slot[slot]
        usage = usage_by_slot[slot]
        token_valid = usage["token_measurement_valid"]
        inference_tokens = usage["inference_tokens"] if token_valid else None
        strict_count = evaluation["MilestoneStrictCount"]
        tokens_per = inference_tokens / strict_count if token_valid and strict_count > 0 else None
        row = {
            "slot": slot,
            "model": expected_model(manifest, slot),
            "InstructionGate": evaluation["InstructionGate"],
            "ClosedLoopProjectCount": evaluation["ClosedLoopProjectCount"],
            "MilestoneStrictCount": strict_count,
            "AcceptanceCoverage": float(evaluation["AcceptanceCoverage"]),
            "InferenceTokens": inference_tokens,
            "InferenceTokensPerMilestoneStrictSuccess": tokens_per,
            "TokenMeasurementStatus": usage["token_measurement_status"],
            "CandidateTokenSoftSLA": bool(token_valid and inference_tokens <= token_sla),
            "DecisionInfrastructureIndeterminate": bool(
                evaluation["decision_infrastructure_indeterminate"]
            ),
            "DecisionFinalist": False,
        }
        rows.append(row)
    return rows


def primary_axes(row: Mapping[str, Any]) -> tuple[int, int, int, float]:
    gate = row["InstructionGate"]
    return (
        int(gate is True),
        int(row["ClosedLoopProjectCount"]),
        int(row["MilestoneStrictCount"]),
        float(row["AcceptanceCoverage"]),
    )


def all_axes(row: Mapping[str, Any]) -> tuple[int, int, int, float, float] | None:
    token_axis = row["InferenceTokensPerMilestoneStrictSuccess"]
    if token_axis is None:
        return None
    return (*primary_axes(row), float(token_axis))


def rank_rows(rows: list[dict[str, Any]], manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    slot_order = {slot: index for index, slot in enumerate(ordered_slots(manifest))}
    grouped: dict[tuple[int, int, int, float], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(primary_axes(row), []).append(row)
    ordered: list[dict[str, Any]] = []
    position = 1
    for prefix in sorted(grouped, reverse=True):
        group = grouped[prefix]
        indeterminate = any(row["InferenceTokensPerMilestoneStrictSuccess"] is None for row in group)
        if indeterminate:
            group.sort(key=lambda row: slot_order[row["slot"]])
            for row in group:
                row["Rank"] = None
                row["RankStatus"] = "indeterminate_within_primary_axes"
                ordered.append(row)
        else:
            group.sort(
                key=lambda row: (
                    row["InferenceTokensPerMilestoneStrictSuccess"],
                    slot_order[row["slot"]],
                )
            )
            previous_token: float | None = None
            token_rank = position
            for offset, row in enumerate(group):
                token = float(row["InferenceTokensPerMilestoneStrictSuccess"])
                if previous_token is None or token != previous_token:
                    token_rank = position + offset
                    previous_token = token
                row["Rank"] = token_rank
                row["RankStatus"] = "ranked"
                ordered.append(row)
        position += len(group)
    return ordered


def select_finalists(rows: list[dict[str, Any]]) -> tuple[str, list[str], list[str]]:
    if not rows:
        return "experiment_incomplete", [], ["no candidate rows"]
    global_infrastructure = [
        row["slot"] for row in rows if row["DecisionInfrastructureIndeterminate"]
    ]
    if global_infrastructure:
        return (
            "decision_infrastructure_indeterminate",
            [],
            [f"decision infrastructure indeterminate for {slot}" for slot in sorted(global_infrastructure)],
        )
    best_primary = max(primary_axes(row) for row in rows)
    top_set = [row for row in rows if primary_axes(row) == best_primary]
    token_indeterminate = [
        row["slot"]
        for row in top_set
        if row["InferenceTokensPerMilestoneStrictSuccess"] is None
    ]
    if token_indeterminate:
        return (
            "token_indeterminate",
            [],
            [
                "top-set token evidence indeterminate for: "
                + ", ".join(sorted(token_indeterminate))
            ],
        )
    best_tokens = min(row["InferenceTokensPerMilestoneStrictSuccess"] for row in top_set)
    finalists = sorted(
        row["slot"]
        for row in top_set
        if row["InferenceTokensPerMilestoneStrictSuccess"] == best_tokens
    )
    return ("unique_finalist" if len(finalists) == 1 else "exact_axis_tie"), finalists, []


def audit_legacy_hashes(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    prototype_root = project_root / "prototype"
    violations: list[str] = []
    files: dict[str, dict[str, Any]] = {}
    workspace_trees: dict[str, dict[str, Any]] = {}

    def checked_relative(base: Path, relative: Any, context: str) -> Path:
        if not isinstance(relative, str):
            raise SchemaError(f"{context} path must be a string")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.as_posix() != relative:
            raise SchemaError(f"{context} contains unsafe path {relative!r}")
        return base / relative_path

    prototype_audit_path = prototype_root / "results" / "audit.json"
    try:
        prototype_audit = load_json(prototype_audit_path)
        expected_files = prototype_audit["files"]
        expected_workspaces = prototype_audit.get("workspace_hashes_after")
        if not isinstance(expected_files, dict) or not isinstance(expected_workspaces, dict):
            raise SchemaError("prototype audit files/workspace_hashes_after must be objects")
        for relative, expected in expected_files.items():
            path = checked_relative(prototype_root, relative, "prototype audit")
            actual = sha256_file(path) if path.is_file() else None
            key = f"prototype/{relative}"
            files[key] = {"expected": expected, "actual": actual, "unchanged": actual == expected}
            if actual != expected:
                violations.append(f"legacy prototype hash mismatch: {key}")
        for slot, expected_map in expected_workspaces.items():
            if not isinstance(slot, str) or not isinstance(expected_map, dict):
                raise SchemaError("prototype workspace hash entry is invalid")
            actual_map = tree_hashes(prototype_root / "runs" / slot / "workspace")
            workspace_trees[f"prototype/{slot}"] = {
                "expected_files": expected_map,
                "actual_files": actual_map,
                "unchanged": actual_map == expected_map,
            }
            if actual_map != expected_map:
                violations.append(f"legacy prototype workspace hash mismatch: {slot}")
    except (OSError, KeyError, TypeError, SchemaError) as error:
        violations.append(f"cannot validate legacy prototype audit: {type(error).__name__}: {error}")

    v4_root = prototype_root / "results" / "behavioral-quality-v4"
    v4_audit_path = v4_root / "audit.json"
    try:
        v4_audit = load_json(v4_audit_path)
        expected_v4: dict[Path, str] = {
            v4_root / "protocol.md": v4_audit["input_hashes"]["protocol.md"],
            v4_root / "probe-manifest.json": v4_audit["input_hashes"]["probe-manifest.json"],
        }
        for name, digest in v4_audit["input_hashes"]["scripts"].items():
            expected_v4[checked_relative(v4_root, name, "v4 script audit")] = digest
        for name, digest in v4_audit["output_hashes"].items():
            expected_v4[checked_relative(v4_root, name, "v4 output audit")] = digest
        for relative, evidence in v4_audit["primary_results"].items():
            expected_v4[checked_relative(project_root, relative, "v4 primary-result audit")] = evidence["after"]
        for path, expected in expected_v4.items():
            actual = sha256_file(path) if path.is_file() else None
            key = path.relative_to(project_root).as_posix()
            files[key] = {"expected": expected, "actual": actual, "unchanged": actual == expected}
            if actual != expected:
                violations.append(f"legacy v4 hash mismatch: {key}")

        v4_trees = v4_audit.get("workspace_trees")
        v4_src = v4_audit.get("workspace_src_hashes")
        if not isinstance(v4_trees, dict) or not isinstance(v4_src, dict):
            raise SchemaError("v4 workspace_trees/workspace_src_hashes must be objects")
        for slot, evidence in v4_trees.items():
            if not isinstance(slot, str) or not isinstance(evidence, dict):
                raise SchemaError("v4 workspace tree entry is invalid")
            before = {
                "file_count": evidence.get("before_file_count"),
                "tree_digest": evidence.get("before_tree_digest"),
                "files": evidence.get("before_files"),
            }
            after = {
                "file_count": evidence.get("after_file_count"),
                "tree_digest": evidence.get("after_tree_digest"),
                "files": evidence.get("after_files"),
            }
            validate_tree_snapshot(before, f"legacy v4 {slot} before")
            validate_tree_snapshot(after, f"legacy v4 {slot} after")
            actual = tree_snapshot(prototype_root / "runs" / slot / "workspace")
            unchanged = actual == after
            workspace_trees[f"behavioral-quality-v4/{slot}"] = {
                "before": before,
                "expected_after": after,
                "actual_after": actual,
                "unchanged": unchanged,
            }
            if not unchanged:
                violations.append(f"legacy v4 workspace hash mismatch: {slot}")
            expected_src = v4_src.get(slot)
            actual_src = {
                path: digest for path, digest in actual["files"].items() if path.startswith("src/")
            }
            if expected_src != actual_src:
                violations.append(f"legacy v4 workspace src hash mismatch: {slot}")
    except (OSError, KeyError, TypeError, SchemaError) as error:
        violations.append(f"cannot validate legacy v4 audit: {type(error).__name__}: {error}")
    return {
        "passed": not violations,
        "violations": violations,
        "files": files,
        "workspace_trees": workspace_trees,
    }


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=LEADERBOARD_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _report_text(
    manifest: Mapping[str, Any],
    rows: list[dict[str, Any]],
    status: str,
    finalists: list[str],
    blockers: list[str],
) -> str:
    axes = " → ".join(axis for axis, _ in RANKING_AXES)
    lines = [
        "# Closed-loop benchmark v2 result",
        "",
        "No subjective or weighted total score is computed.",
        "",
        f"- Decision status: `{status}`",
        f"- Finalist set: {', '.join(finalists) if finalists else 'not published'}",
        f"- Mechanical axes: `{axes}` (the final axis is ascending; all prior axes are descending)",
        "",
    ]
    if blockers:
        lines.extend(("## Publication blockers", ""))
        lines.extend(f"- {blocker}" for blocker in blockers)
        lines.append("")
    lines.extend(
        (
            "## Lexicographic leaderboard",
            "",
            "| Rank | Slot | Model | InstructionGate | Closed projects | Strict milestones | Coverage | Inference tokens / strict milestone | Token SLA |",
            "|---:|---|---|---|---:|---:|---:|---:|---|",
        )
    )
    for row in rows:
        gate = "indeterminate" if row["InstructionGate"] is None else ("PASS" if row["InstructionGate"] else "FAIL")
        token_axis = row["InferenceTokensPerMilestoneStrictSuccess"]
        token_text = "indeterminate" if token_axis is None else f"{token_axis:.12g}"
        rank_text = "indeterminate" if row["Rank"] is None else str(row["Rank"])
        lines.append(
            f"| {rank_text} | {row['slot']} | `{row['model']}` | {gate} | "
            f"{row['ClosedLoopProjectCount']}/{len(manifest['projects'])} | "
            f"{row['MilestoneStrictCount']}/{manifest['milestone_count']} | "
            f"{row['AcceptanceCoverage']:.12g} | {token_text} | "
            f"{'PASS' if row['CandidateTokenSoftSLA'] else 'not met/indeterminate'} |"
        )
    lines.extend(
        (
            "",
            "Rows retain the same rank only when every decision axis is exactly equal. If the final token axis is indeterminate within an otherwise equal primary-axis group, the group is explicitly not force-ordered.",
            "Main-agent/planner usage is excluded from candidate ranking and candidate SLA calculations.",
            "",
        )
    )
    return "\n".join(lines)


def build_report(results_dir: Path, root: Path = CLOSED_LOOP_ROOT) -> dict[str, Any]:
    manifest = load_manifest(root)
    criteria_manifest = load_criteria(root, manifest)
    evaluations = load_jsonl(results_dir / "evaluation.jsonl")
    usages = load_jsonl(results_dir / "usage.jsonl")
    rows = build_rows(manifest, evaluations, usages, criteria_manifest)
    rows = rank_rows(rows, manifest)
    status, finalists, blockers = select_finalists(rows)
    asset_violations = verify_assets(root)
    legacy_audit = audit_legacy_hashes(root.parent)
    if asset_violations:
        blockers.extend(f"asset integrity: {violation}" for violation in asset_violations)
    if not legacy_audit["passed"]:
        blockers.extend(legacy_audit["violations"])
    if blockers and status not in {"decision_infrastructure_indeterminate", "token_indeterminate"}:
        status = "integrity_indeterminate"
        finalists = []
    for row in rows:
        row["DecisionFinalist"] = row["slot"] in finalists

    atomic_write_bytes(results_dir / "leaderboard.csv", _csv_bytes(rows))
    report_text = _report_text(manifest, rows, status, finalists, blockers)
    atomic_write_bytes(results_dir / "report.md", report_text.encode("utf-8"))
    decision = {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": utc_now(),
        "decision_status": status,
        "finalists": finalists,
        "publication_blockers": blockers,
        "ranking_axes": [{"id": axis, "direction": direction} for axis, direction in RANKING_AXES],
        "ties": "preserve_when_all_axes_equal",
        "subjective_or_weighted_quality_score": False,
    }
    atomic_write_json(results_dir / "final-decision.json", decision)
    audit = {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": utc_now(),
        "decision_status": status,
        "finalists": finalists,
        "asset_integrity": {"passed": not asset_violations, "violations": asset_violations},
        "legacy_prototype_v4_hash_audit": legacy_audit,
        "workspace_trees": {
            record["slot"]: {
                "before": record["workspace_before"],
                "after": record["workspace_after"],
                "changed_paths": record["workspace_changed_paths"],
            }
            for record in evaluations
        },
        "output_hashes": {
            name: sha256_file(results_dir / name)
            for name in ("leaderboard.csv", "report.md", "final-decision.json")
        },
        "self_hash_omitted": True,
    }
    atomic_write_json(results_dir / "audit.json", audit)
    return decision


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the closed-loop v2 mechanical leaderboard and audit.")
    parser.add_argument("--results-dir", type=Path, default=CLOSED_LOOP_ROOT / "results")
    args = parser.parse_args()
    print(json.dumps(build_report(args.results_dir), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
