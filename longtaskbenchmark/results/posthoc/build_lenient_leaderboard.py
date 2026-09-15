from __future__ import annotations

import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "results" / "posthoc"
sys.path.insert(0, str(ROOT / "harness"))

from common import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    load_json,
    load_jsonl,
    load_manifest,
    ordered_slots,
    sha256_file,
    utc_now,
)


AXES = (
    ("ExecutionCompleted", "desc"),
    ("RawMilestoneCount", "desc"),
    ("AcceptanceCoverage", "desc"),
    ("InstructionComplianceRate", "desc"),
    ("InferenceTokensPerPassedCriterion", "asc"),
)


def _raw_milestones(record: dict[str, Any]) -> int:
    return sum(
        all(criterion["status"] == "passed" for criterion in milestone["criteria"])
        for project in record["projects"]
        for milestone in project["milestones"]
    )


def _instruction_compliance(record: dict[str, Any]) -> tuple[int, int, float]:
    observable = [item for item in record["instruction"]["checks"] if item["observable"]]
    passed = sum(item["status"] == "passed" for item in observable)
    total = len(observable)
    if total == 0:
        return 0, 0, 0.0
    return passed, total, passed / total


def _axis(row: dict[str, Any]) -> tuple[int, int, float, float, float]:
    token_axis = row["InferenceTokensPerPassedCriterion"]
    return (
        int(row["ExecutionCompleted"]),
        int(row["RawMilestoneCount"]),
        float(row["AcceptanceCoverage"]),
        float(row["InstructionComplianceRate"]),
        -float(token_axis) if token_axis is not None else -math.inf,
    )


def build() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = load_manifest(ROOT)
    evaluations = load_jsonl(OUTPUT_DIR / "evaluation-rescored.jsonl")
    usages = load_jsonl(ROOT / "results" / "usage.jsonl")
    agent_map = load_json(ROOT / "results" / "agent-map.json")

    evaluation_by_slot = {item["slot"]: item for item in evaluations}
    usage_by_slot = {item["slot"]: item for item in usages}
    executor_by_slot = {item["slot"]: item for item in agent_map["executors"]}
    expected_slots = set(manifest["candidate_slots"])
    if (
        set(evaluation_by_slot) != expected_slots
        or set(usage_by_slot) != expected_slots
        or set(executor_by_slot) != expected_slots
    ):
        raise RuntimeError("lenient inputs must cover every manifest slot exactly once")

    rows: list[dict[str, Any]] = []
    for slot in ordered_slots(manifest):
        evaluation = evaluation_by_slot[slot]
        usage = usage_by_slot[slot]
        executor = executor_by_slot[slot]
        execution_completed = executor["status"] == "completed"
        passed_criteria = int(evaluation["criterion_pass_count"])
        inference_tokens = usage["inference_tokens"] if usage["token_measurement_valid"] else None
        tokens_per_criterion = (
            inference_tokens / passed_criteria
            if execution_completed and inference_tokens is not None and passed_criteria > 0
            else None
        )
        instruction_passed, instruction_total, instruction_rate = _instruction_compliance(
            evaluation
        )
        rows.append(
            {
                "Rank": None,
                "RankStatus": "pending",
                "Slot": slot,
                "Model": evaluation["model"],
                "ExecutionCompleted": execution_completed,
                "StrictInstructionGate": evaluation["InstructionGate"],
                "RawMilestoneCount": _raw_milestones(evaluation),
                "PassedCriteria": passed_criteria,
                "AcceptanceCoverage": float(evaluation["AcceptanceCoverage"]),
                "InstructionChecksPassed": instruction_passed,
                "InstructionChecksTotal": instruction_total,
                "InstructionComplianceRate": instruction_rate,
                "InferenceTokens": inference_tokens,
                "InferenceTokensPerPassedCriterion": tokens_per_criterion,
            }
        )

    completed = [row for row in rows if row["ExecutionCompleted"]]
    incomplete = [row for row in rows if not row["ExecutionCompleted"]]
    completed.sort(key=_axis, reverse=True)
    previous_axis: tuple[int, int, float, float, float] | None = None
    current_rank = 0
    for position, row in enumerate(completed, start=1):
        axis = _axis(row)
        if axis != previous_axis:
            current_rank = position
            previous_axis = axis
        row["Rank"] = current_rank
        row["RankStatus"] = "ranked_lenient"
    for row in incomplete:
        row["RankStatus"] = "not_ranked_no_model_execution"
    rows = completed + incomplete

    decision = {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": utc_now(),
        "status": "exploratory_lenient_secondary_view",
        "changes_strict_result": False,
        "axes": [{"id": axis, "direction": direction} for axis, direction in AXES],
        "rule": (
            "InstructionGate is not a hard zero. Raw milestones count when both criteria pass "
            "regardless of gate; observable instruction compliance remains a lower-priority axis."
        ),
        "unranked_slots": [row["Slot"] for row in incomplete],
        "top_ranked_slot": completed[0]["Slot"] if completed else None,
    }
    return rows, decision


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _report(rows: list[dict[str, Any]], decision: dict[str, Any]) -> str:
    lines = [
        "# Closed-loop v2 lenient secondary leaderboard",
        "",
        "This is a secondary exploratory view. It does not replace the strict leaderboard or publish a winner.",
        "",
        "## Lenient rule",
        "",
        "1. A slot must have an actual completed model execution.",
        "2. `RawMilestoneCount`: a milestone counts when both criteria pass, even if InstructionGate failed.",
        "3. `AcceptanceCoverage`: passed criteria divided by 20.",
        "4. `InstructionComplianceRate`: observable instruction checks passed; this is a secondary axis rather than a hard gate.",
        "5. `InferenceTokensPerPassedCriterion`: ascending efficiency tie-breaker.",
        "",
        "Mechanical order: `ExecutionCompleted → RawMilestoneCount → AcceptanceCoverage → InstructionComplianceRate → InferenceTokensPerPassedCriterion`.",
        "",
        "## Ranking",
        "",
        "| Rank | Slot | Model | Raw milestones | Coverage | Strict gate | Instruction checks | Tokens / passed criterion |",
        "|---:|---|---|---:|---:|---|---:|---:|",
    ]
    for row in rows:
        rank = row["Rank"] if row["Rank"] is not None else "not ranked"
        gate = "PASS" if row["StrictInstructionGate"] is True else "FAIL"
        if row["StrictInstructionGate"] is None:
            gate = "indeterminate"
        token_axis = row["InferenceTokensPerPassedCriterion"]
        token_text = "indeterminate" if token_axis is None else f"{token_axis:.2f}"
        lines.append(
            f"| {rank} | {row['Slot']} | `{row['Model']}` | "
            f"{row['RawMilestoneCount']}/10 | {row['AcceptanceCoverage']:.2f} | {gate} | "
            f"{row['InstructionChecksPassed']}/{row['InstructionChecksTotal']} | {token_text} |"
        )
    lines.extend(
        (
            "",
            "Gate-failing candidates receive business-completion credit in this view, but their instruction violations remain visible. A slot without any model response is not assigned a rank.",
            "",
        )
    )
    return "\n".join(lines)


def main() -> None:
    rows, decision = build()
    csv_path = OUTPUT_DIR / "leaderboard-lenient.csv"
    report_path = OUTPUT_DIR / "report-lenient.md"
    decision_path = OUTPUT_DIR / "lenient-decision.json"
    atomic_write_bytes(csv_path, _csv_bytes(rows))
    atomic_write_bytes(report_path, _report(rows, decision).encode("utf-8"))
    atomic_write_json(decision_path, decision)
    audit = {
        "generated_at": utc_now(),
        "input_hashes": {
            "evaluation_rescored": sha256_file(OUTPUT_DIR / "evaluation-rescored.jsonl"),
            "usage": sha256_file(ROOT / "results" / "usage.jsonl"),
            "agent_map": sha256_file(ROOT / "results" / "agent-map.json"),
            "script": sha256_file(Path(__file__)),
        },
        "output_hashes": {
            csv_path.name: sha256_file(csv_path),
            report_path.name: sha256_file(report_path),
            decision_path.name: sha256_file(decision_path),
        },
    }
    atomic_write_json(OUTPUT_DIR / "lenient-audit.json", audit)
    print(
        json.dumps(
            {
                "ranked": sum(row["Rank"] is not None for row in rows),
                "unranked": decision["unranked_slots"],
                "top_three": [row["Slot"] for row in rows if row["Rank"] is not None][:3],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
