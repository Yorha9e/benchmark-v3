"""Rebuild condition-specific rankings and A/B deltas from raw evaluations."""

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from common import atomic_json, load_manifest


def _load_inputs(path):
    path = pathlib.Path(path)
    paths = sorted(path.glob("*.json")) if path.is_dir() else [path]
    values = []
    for item in paths:
        with item.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        values.extend(value if isinstance(value, list) else [value])
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("each evaluation must be an object")
        identity = (value.get("condition"), value.get("slot"))
        if identity in seen:
            raise ValueError(f"duplicate evaluation cell: {identity}")
        seen.add(identity)
    return values


def _axis_key(entry, board, axes):
    metrics = entry.get(board, {})
    key = []
    for axis in axes:
        value = metrics.get(axis["name"])
        if value is None:
            key.append((1, 0))
        elif axis["direction"] == "high":
            key.append((0, -value))
        else:
            key.append((0, value))
    return tuple(key)


def _rank(entries, board, axes):
    ordered = sorted(entries, key=lambda entry: _axis_key(entry, board, axes))
    output = []
    previous_key = None
    previous_rank = 0
    for index, entry in enumerate(ordered, 1):
        key = _axis_key(entry, board, axes)
        if key != previous_key:
            previous_rank = index
            previous_key = key
        output.append({
            "condition": entry["condition"],
            "slot": entry["slot"],
            "model": entry.get("model", entry["slot"]),
            "rank": previous_rank,
            "metrics": entry[board],
        })
    return output


def _delta(a, b, usage_a=None, usage_b=None, names=None):
    names = names or ("StrictTaskCount", "OfficialCriterionCount", "InstructionGate", "InferenceTokensPerStrictTask", "InferenceTokensPerPassedCriterion")
    delta = {}
    for name in names:
        left = a.get(name)
        right = b.get(name)
        if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
            delta[name] = right - left
        else:
            delta[name] = None
    usage_a = usage_a or {}
    usage_b = usage_b or {}
    delta["B_minus_A_usage_total_tokens"] = usage_b.get("total_tokens", 0) - usage_a.get("total_tokens", 0)
    return delta


def build_report(input_path, manifest_path=None):
    manifest_root = pathlib.Path(manifest_path).resolve().parent if manifest_path else pathlib.Path(__file__).resolve().parents[1]
    manifest = load_manifest(manifest_root)
    entries = _load_inputs(input_path)
    expected_id = manifest["benchmark_id"]
    for entry in entries:
        if entry.get("benchmark_id") != expected_id:
            raise ValueError("input benchmark id mismatch")
        if entry.get("condition") not in manifest["conditions"]:
            raise ValueError("input condition is not in manifest")
        if not isinstance(entry.get("strict"), dict) or not isinstance(entry.get("lenient"), dict):
            raise ValueError("evaluation metrics are missing")

    rankings = {"strict": {}, "lenient": {}}
    for board in ("strict", "lenient"):
        axes = manifest["ranking_axes"][board]
        for condition in manifest["conditions"]:
            rankings[board][condition] = _rank(
                [entry for entry in entries if entry["condition"] == condition],
                board,
                axes,
            )

    by_model = {}
    for entry in entries:
        by_model.setdefault(entry.get("model", entry["slot"]), {})[entry["condition"]] = entry
    deltas = []
    for model in sorted(by_model):
        pair = by_model[model]
        if "A" not in pair or "B" not in pair:
            continue
        deltas.append({
            "model": model,
            "A_slot": pair["A"]["slot"],
            "B_slot": pair["B"]["slot"],
            "strict": _delta(pair["A"]["strict"], pair["B"]["strict"], pair["A"].get("usage"), pair["B"].get("usage")),
            "lenient": _delta(pair["A"]["lenient"], pair["B"]["lenient"], pair["A"].get("usage"), pair["B"].get("usage"), ("ExecutionCompleted", "RawCriterionCount", "AcceptanceCoverage", "InstructionComplianceRate", "ExtensionCapabilityCount", "InferenceTokensPerPassedCriterion")),
            "A_usage": pair["A"].get("usage", {}),
            "B_usage": pair["B"].get("usage", {}),
        })

    planner_usage_path = manifest_root / "results" / "planner-usage.json"
    planner_usage = None
    if planner_usage_path.is_file():
        with planner_usage_path.open("r", encoding="utf-8") as handle:
            planner_usage = json.load(handle)
    planner = {
        "status": "completed" if planner_usage and planner_usage.get("available") else "not_run",
        "tokens": planner_usage.get("total_tokens") if planner_usage else None,
        "usage": planner_usage,
        "soft_budget": manifest.get("resource_sla", {}).get("planner_tokens_soft"),
    }

    return {
        "schema_version": 1,
        "benchmark_id": expected_id,
        "entry_count": len(entries),
        "conditions": {condition: sum(entry["condition"] == condition for entry in entries) for condition in manifest["conditions"]},
        "ranking_axes": manifest["ranking_axes"],
        "rankings": rankings,
        "ab_delta": deltas,
        "planner": planner,
        "calibration": manifest.get("status", {}),
        "formal_ranking_available": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest")
    args = parser.parse_args()
    try:
        report = build_report(args.input, args.manifest)
        atomic_json(args.output, report)
    except (OSError, ValueError) as error:
        parser.exit(2, f"report failed: {error}\n")


if __name__ == "__main__":
    main()
