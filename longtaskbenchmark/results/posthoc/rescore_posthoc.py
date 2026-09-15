from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POSTHOC_DIR = ROOT / "results" / "posthoc"
sys.path.insert(0, str(ROOT / "harness"))

from build_report import rank_rows, select_finalists  # noqa: E402
from common import (  # noqa: E402
    RANKING_AXES,
    SchemaError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_jsonl,
    load_json,
    load_jsonl,
    load_manifest,
    ordered_slots,
    sha256_file,
    tree_hashes,
    utc_now,
    verify_assets,
)
from compare_rounds import (  # noqa: E402
    load_behavioral_v4,
    load_prototype_v1,
    pairwise_concordance,
    top_set_movement,
    _report as compare_report_text,
)
from instruction_audit import check  # noqa: E402


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


def _completion_workspace_snapshots() -> dict[str, dict[str, str]]:
    snapshots: dict[str, dict[str, str]] = {}
    for name in ("wave1-completions.json", "wave2-completions.json"):
        payload = load_json(ROOT / "results" / name)
        for executor in payload["executors"]:
            slot = executor["slot"]
            if slot in snapshots:
                raise SchemaError(f"duplicate completion workspace snapshot: {slot}")
            snapshot = executor["workspace_after_candidate"]
            files = snapshot.get("files") if isinstance(snapshot, dict) else None
            if not isinstance(files, dict) or any(
                not isinstance(path, str) or not isinstance(digest, str)
                for path, digest in files.items()
            ):
                raise SchemaError(f"invalid completion workspace snapshot for {slot}")
            snapshots[slot] = dict(files)
    return snapshots


def _fixed_workspace_non_source(
    workspace: Path,
    slot: str,
    snapshots: dict[str, dict[str, str]],
) -> list[str]:
    if slot not in snapshots:
        raise SchemaError(f"missing completion workspace snapshot: {slot}")
    workspace_hashes = snapshots[slot]
    current_hashes = tree_hashes(workspace)
    if current_hashes != workspace_hashes:
        raise RuntimeError(f"workspace changed after completion snapshot: {slot}")

    template_hashes = tree_hashes(ROOT / "template")
    allowed_source = ("src/order_fulfillment/", "src/delivery_spool/")

    def is_allowed_source(relative: str) -> bool:
        return any(relative.startswith(prefix) for prefix in allowed_source)

    expected = {
        path: digest for path, digest in template_hashes.items() if not is_allowed_source(path)
    }
    actual = {
        path: digest for path, digest in workspace_hashes.items() if not is_allowed_source(path)
    }
    violations: list[str] = []
    for relative, digest in expected.items():
        if relative not in actual:
            violations.append(f"missing workspace asset: {relative}")
        elif actual[relative] != digest:
            violations.append(f"changed workspace asset: {relative}")
    for relative in sorted(set(actual) - set(expected)):
        violations.append(f"unauthorized workspace file: {relative}")
    for relative in workspace_hashes:
        if (
            relative.startswith("src/")
            and not is_allowed_source(relative)
            and relative != "src/__init__.py"
        ):
            violations.append(f"source outside allowed package boundaries: {relative}")
    return sorted(set(violations))


def _replace_check(checks: list[dict[str, Any]], replacement: dict[str, Any]) -> list[dict[str, Any]]:
    return [replacement if item["id"] == replacement["id"] else item for item in checks]


def _fixed_instruction(
    record: dict[str, Any],
    snapshots: dict[str, dict[str, str]],
) -> dict[str, Any]:
    instruction = record["instruction"]
    checks = [dict(item) for item in instruction["checks"]]

    workspace_violations = _fixed_workspace_non_source(
        Path(record["workspace"]), record["slot"], snapshots
    )
    checks = _replace_check(
        checks,
        check(
            "workspace_non_source",
            "passed" if not workspace_violations else "failed",
            workspace_violations,
        ),
    )

    old_import = next(item for item in checks if item["id"] == "standard_library_only_imports")
    old_diagnostics = old_import["diagnostics"]
    fixed_violations = [
        item for item in old_diagnostics.get("violations", []) if item.get("root") != "src"
    ]
    fixed_diagnostics = {
        "imports": old_diagnostics.get("imports", {}),
        "violations": fixed_violations,
        "posthoc_allowed_internal_import_roots": ["src"],
    }
    checks = _replace_check(
        checks,
        check(
            "standard_library_only_imports",
            "passed" if not fixed_violations else "failed",
            fixed_diagnostics,
        ),
    )

    observable_statuses = [item["status"] for item in checks if item["observable"]]
    if "infrastructure_indeterminate" in observable_statuses:
        gate = None
    else:
        gate = all(status == "passed" for status in observable_statuses)
    return {
        "schema_version": instruction["schema_version"],
        "benchmark_version": instruction["benchmark_version"],
        "slot": instruction["slot"],
        "workspace": instruction["workspace"],
        "checks": checks,
        "instruction_gate": gate,
        "decision_infrastructure_indeterminate": gate is None,
    }


def _rescore_record(
    record: dict[str, Any],
    criterion_count: int,
    snapshots: dict[str, dict[str, str]],
) -> dict[str, Any]:
    rescored = json.loads(json.dumps(record))
    instruction = _fixed_instruction(record, snapshots)
    gate = instruction["instruction_gate"]
    rescored["instruction"] = instruction
    rescored["InstructionGate"] = gate

    project_count = 0
    milestone_count = 0
    criterion_pass_count = 0
    indeterminate_criteria: list[str] = []
    for project in rescored["projects"]:
        project_success = True
        for milestone in project["milestones"]:
            statuses = []
            for criterion in milestone["criteria"]:
                statuses.append(criterion["status"])
                criterion_pass_count += int(criterion["status"] == "passed")
                if criterion["status"] == "infrastructure_indeterminate":
                    indeterminate_criteria.append(criterion["id"])
            milestone_success = gate is True and all(status == "passed" for status in statuses)
            milestone["strict_success"] = milestone_success
            milestone_count += int(milestone_success)
            project_success = project_success and milestone_success
        project["closed_loop_success"] = project_success
        project_count += int(project_success)

    rescored["ClosedLoopProjectCount"] = project_count
    rescored["MilestoneStrictCount"] = milestone_count
    rescored["criterion_pass_count"] = criterion_pass_count
    rescored["AcceptanceCoverage"] = criterion_pass_count / criterion_count
    infrastructure = (
        gate is None
        or instruction["decision_infrastructure_indeterminate"]
        or bool(indeterminate_criteria)
    )
    rescored["decision_infrastructure_indeterminate"] = infrastructure
    evidence = sorted(set(indeterminate_criteria))
    if gate is None:
        evidence.append("instruction_audit")
    rescored["infrastructure_indeterminate_evidence"] = evidence
    return rescored


def _rows(records: list[dict[str, Any]], usages: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    usage_by_slot = {item["slot"]: item for item in usages}
    token_sla = manifest["resource_sla"]["candidate_inference_tokens_soft"]
    rows: list[dict[str, Any]] = []
    for record in records:
        slot = record["slot"]
        usage = usage_by_slot[slot]
        token_valid = usage["token_measurement_valid"]
        inference_tokens = usage["inference_tokens"] if token_valid else None
        strict_count = record["MilestoneStrictCount"]
        tokens_per = inference_tokens / strict_count if token_valid and strict_count > 0 else None
        rows.append(
            {
                "slot": slot,
                "model": record["model"],
                "InstructionGate": record["InstructionGate"],
                "ClosedLoopProjectCount": record["ClosedLoopProjectCount"],
                "MilestoneStrictCount": strict_count,
                "AcceptanceCoverage": record["AcceptanceCoverage"],
                "InferenceTokens": inference_tokens,
                "InferenceTokensPerMilestoneStrictSuccess": tokens_per,
                "TokenMeasurementStatus": usage["token_measurement_status"],
                "CandidateTokenSoftSLA": bool(token_valid and inference_tokens <= token_sla),
                "DecisionInfrastructureIndeterminate": record[
                    "decision_infrastructure_indeterminate"
                ],
                "DecisionFinalist": False,
            }
        )
    return rank_rows(rows, manifest)


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=LEADERBOARD_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "Rank": "" if row["Rank"] is None else row["Rank"],
                "RankStatus": row["RankStatus"],
                "Slot": row["slot"],
                "Model": row["model"],
                "InstructionGate": row["InstructionGate"],
                "ClosedLoopProjectCount": row["ClosedLoopProjectCount"],
                "MilestoneStrictCount": row["MilestoneStrictCount"],
                "AcceptanceCoverage": row["AcceptanceCoverage"],
                "InferenceTokens": "" if row["InferenceTokens"] is None else row["InferenceTokens"],
                "InferenceTokensPerMilestoneStrictSuccess": ""
                if row["InferenceTokensPerMilestoneStrictSuccess"] is None
                else row["InferenceTokensPerMilestoneStrictSuccess"],
                "TokenMeasurementStatus": row["TokenMeasurementStatus"],
                "CandidateTokenSoftSLA": row["CandidateTokenSoftSLA"],
                "DecisionInfrastructureIndeterminate": row["DecisionInfrastructureIndeterminate"],
                "DecisionFinalist": row["DecisionFinalist"],
            }
        )
    return buffer.getvalue().encode("utf-8-sig")


def _report_text(
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    status: str,
    finalists: list[str],
    blockers: list[str],
) -> str:
    axes = " → ".join(axis for axis, _ in RANKING_AXES)
    lines = [
        "# Closed-loop benchmark v2 post-hoc rescore",
        "",
        "This is an exploratory post-hoc rescore, not a strict frozen-protocol publication.",
        "Candidate workspaces and token evidence were preserved; only evaluator/report defects were repaired before rescoring.",
        "",
        f"- Decision status: `{status}`",
        f"- Finalist set: {', '.join(finalists) if finalists else 'not published'}",
        f"- Mechanical axes: `{axes}` (the final axis is ascending; all prior axes are descending)",
        "",
        "## Post-hoc repairs applied",
        "",
        "- The template-owned `src/__init__.py` is no longer treated as unauthorized candidate source.",
        "- Internal `src.*` imports are no longer treated as third-party imports.",
        "- The post-hoc leaderboard writes `Slot` and `Model` into the correct CSV columns.",
        "- Workspace checks are bound to the saved completion snapshots, and current workspaces are verified to match them.",
        "- Cross-round output separates the mechanical top set from the still-empty published finalist set.",
        "",
        "## Publication blockers",
        "",
    ]
    lines.extend(f"- {blocker}" for blocker in blockers)
    lines.extend(
        (
            "",
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
            "Rows retain the same rank only when every decision axis is exactly equal. The original frozen-protocol outputs remain in `closed_loop_v2/results/`; these post-hoc outputs are exploratory.",
            "Main-agent/planner usage remains excluded from candidate ranking and candidate SLA calculations.",
            "",
        )
    )
    return "\n".join(lines)


def _write_compare_rounds(
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    decision_status: str,
    mechanical_top_set: list[str],
) -> dict[str, Any]:
    slots = ordered_slots(manifest)
    v2_round = {
        "round": "closed-loop-v2-posthoc",
        "native_axes": [
            "InstructionGate(desc)",
            "ClosedLoopProjectCount(desc)",
            "MilestoneStrictCount(desc)",
            "AcceptanceCoverage(desc)",
            "InferenceTokensPerMilestoneStrictSuccess(asc)",
        ],
        "rank_semantics": "post-hoc repaired v2 leaderboard; not a strict frozen-protocol publication",
        "ranks": {row["slot"]: row["Rank"] for row in rows},
        "rank_statuses": {row["slot"]: row["RankStatus"] for row in rows},
        "top_set": sorted(mechanical_top_set),
        "top_set_semantics": "mechanical_rank_top_set_not_published_finalists",
        "published_finalists": [],
        "decision_status": decision_status,
    }
    rounds = [
        load_prototype_v1(ROOT.parent, slots),
        load_behavioral_v4(ROOT.parent, slots),
        v2_round,
    ]
    movement: list[dict[str, Any]] = []
    for previous, current in zip(rounds, rounds[1:]):
        movement.extend(top_set_movement(previous, current, slots))
    payload = {
        "schema_version": manifest["schema_version"],
        "generated_at": utc_now(),
        "round_order": ["prototype-v1", "behavioral-quality-v4", "closed-loop-v2-posthoc"],
        "score_merging": False,
        "posthoc_rescore": True,
        "top_set_semantics": "closed-loop-v2-posthoc top_set is mechanical only; published finalists remain empty",
        "rounds": rounds,
        "movement": movement,
        "pairwise_concordance": pairwise_concordance(rounds, slots),
        "publication_blocked": True,
    }
    atomic_write_json(POSTHOC_DIR / "compare-rounds-posthoc.json", payload)
    compare_note = (
        "Note: for `closed-loop-v2-posthoc`, `Top set` below is the mechanical rank top set before "
        "provider/incompleteness override; no finalist is published.\n\n"
    )
    atomic_write_bytes(
        POSTHOC_DIR / "compare-rounds-posthoc.md",
        (compare_note + compare_report_text(payload)).encode("utf-8"),
    )
    return payload


def main() -> None:
    asset_violations = verify_assets(ROOT)
    if asset_violations:
        raise RuntimeError("controlled asset verification failed: " + "; ".join(asset_violations))

    manifest = load_manifest(ROOT)
    criterion_count = manifest["criterion_count"]
    snapshots = _completion_workspace_snapshots()
    if set(snapshots) != set(manifest["candidate_slots"]):
        raise SchemaError("completion workspace snapshots must cover every candidate slot exactly once")
    records = [
        _rescore_record(record, criterion_count, snapshots)
        for record in load_jsonl(ROOT / "results" / "evaluation.jsonl")
    ]
    usages = load_jsonl(ROOT / "results" / "usage.jsonl")
    rows = _rows(records, usages, manifest)
    raw_status, raw_finalists, raw_blockers = select_finalists(rows)

    provider_failure = load_json(ROOT / "results" / "subtest_7-provider-failure.json")
    failure_chain = provider_failure.get("failure_chain")
    provider_blocked = (
        provider_failure.get("slot") == "subtest_7"
        and provider_failure.get("expected_model")
        == manifest["candidate_slots"]["subtest_7"]["model"]
        and provider_failure.get("status") == "provider_blocked_before_model_response"
        and isinstance(failure_chain, list)
        and len(failure_chain) == 2
        and [item.get("attempt") for item in failure_chain] == [0, 1]
        and all(item.get("model_response_started") is False for item in failure_chain)
        and all(item.get("usage_record_count") == 0 for item in failure_chain)
    )
    if provider_failure.get("status") and not provider_blocked:
        raise SchemaError("provider failure evidence does not prove the allowed replacement chain")

    blockers = [
        "post-hoc evaluator repair applied after candidate execution; this is not a strict frozen-protocol publication",
        "subtest_7 provider blocked before model response after the allowed single replacement",
        *raw_blockers,
    ]
    if provider_blocked:
        status = "experiment_incomplete_posthoc_rescore"
        finalists: list[str] = []
    else:
        status = raw_status
        finalists = raw_finalists
    for row in rows:
        row["DecisionFinalist"] = row["slot"] in finalists

    POSTHOC_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_jsonl(POSTHOC_DIR / "evaluation-rescored.jsonl", records)
    atomic_write_bytes(POSTHOC_DIR / "leaderboard-rescored.csv", _csv_bytes(rows))
    atomic_write_bytes(
        POSTHOC_DIR / "report-rescored.md",
        _report_text(manifest, rows, status, finalists, blockers).encode("utf-8"),
    )
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
        "posthoc_rescore": True,
        "strict_frozen_protocol_publication": False,
    }
    atomic_write_json(POSTHOC_DIR / "final-decision-rescored.json", decision)
    compare_payload = _write_compare_rounds(manifest, rows, status, raw_finalists)
    output_names = (
        "evaluation-rescored.jsonl",
        "leaderboard-rescored.csv",
        "report-rescored.md",
        "final-decision-rescored.json",
        "compare-rounds-posthoc.json",
        "compare-rounds-posthoc.md",
    )
    audit = {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": utc_now(),
        "posthoc_rescore": True,
        "strict_frozen_protocol_publication": False,
        "controlled_asset_verification": "passed",
        "workspace_snapshots_bound_to_completion_ledgers": True,
        "repairs": [
            "allow template-owned src/__init__.py",
            "allow internal src.* imports in standard-library-only import check",
            "emit Slot and Model in the correct leaderboard CSV columns",
            "bind workspace checks to completion snapshots and verify current workspaces match them",
            "separate mechanical top set from unpublished finalists in compare-rounds",
        ],
        "input_hashes": {
            "rescore_script": sha256_file(POSTHOC_DIR / "rescore_posthoc.py"),
            "manifest": sha256_file(ROOT / "manifest.json"),
            "criteria": sha256_file(ROOT / "evaluator" / "criteria.json"),
            "asset_hashes": sha256_file(ROOT / "asset-hashes.json"),
            "evaluation": sha256_file(ROOT / "results" / "evaluation.jsonl"),
            "evaluation_audit": sha256_file(ROOT / "results" / "evaluation-audit.json"),
            "usage": sha256_file(ROOT / "results" / "usage.jsonl"),
            "agent_map": sha256_file(ROOT / "results" / "agent-map.json"),
            "wave1_completions": sha256_file(ROOT / "results" / "wave1-completions.json"),
            "wave2_completions": sha256_file(ROOT / "results" / "wave2-completions.json"),
            "subtest_7_provider_failure": sha256_file(
                ROOT / "results" / "subtest_7-provider-failure.json"
            ),
        },
        "output_hashes": {
            name: sha256_file(POSTHOC_DIR / name) for name in output_names
        },
        "raw_decision_status_before_provider_override": raw_status,
        "raw_finalists_before_provider_override": raw_finalists,
        "compare_rounds_generated": bool(compare_payload),
    }
    atomic_write_json(POSTHOC_DIR / "rescore-audit.json", audit)
    print(
        json.dumps(
            {
                "posthoc_records": len(records),
                "decision_status": status,
                "raw_status_before_provider_override": raw_status,
                "raw_finalists_before_provider_override": raw_finalists,
                "top_ranked_slots": [row["slot"] for row in rows[:3]],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
