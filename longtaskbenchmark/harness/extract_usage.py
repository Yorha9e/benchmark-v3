from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    CLOSED_LOOP_ROOT,
    SchemaError,
    atomic_write_json,
    atomic_write_jsonl,
    finite_nonnegative_integer,
    load_json,
    load_manifest,
    validate_agent_map,
)


USAGE_FIELDS = ("inputOther", "inputCacheRead", "inputCacheCreation", "output")


def aggregate_wire(path: Path, expected_model: str) -> dict[str, Any]:
    invalid_reasons: list[str] = []
    totals = {field: 0 for field in USAGE_FIELDS}
    record_count = 0
    models: set[str] = set()
    if not path.is_file():
        invalid_reasons.append(f"wire file missing: {path}")
    else:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            lines = []
            invalid_reasons.append(f"cannot read wire file: {type(error).__name__}: {error}")
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                invalid_reasons.append(f"blank wire record at line {line_number}")
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                invalid_reasons.append(f"invalid JSON at line {line_number}: {error}")
                continue
            if not isinstance(event, dict):
                invalid_reasons.append(f"wire record at line {line_number} is not an object")
                continue
            if event.get("type") != "usage.record":
                continue
            record_count += 1
            model = event.get("model")
            usage = event.get("usage")
            if not isinstance(model, str) or not model:
                invalid_reasons.append(f"usage record {record_count} has invalid model")
            else:
                models.add(model)
            if not isinstance(usage, dict):
                invalid_reasons.append(f"usage record {record_count} usage is not an object")
                continue
            for field in USAGE_FIELDS:
                value = usage.get(field)
                if not finite_nonnegative_integer(value):
                    invalid_reasons.append(f"usage record {record_count} field {field} is not a non-negative integer")
                else:
                    totals[field] += value
    if record_count == 0:
        invalid_reasons.append("no usage.record events")
    if models != {expected_model}:
        invalid_reasons.append(f"reported models {sorted(models)!r} do not exactly match expected model {expected_model!r}")

    input_context = totals["inputOther"] + totals["inputCacheRead"] + totals["inputCacheCreation"]
    output_tokens = totals["output"]
    valid = not invalid_reasons
    return {
        "token_measurement_status": "valid" if valid else "indeterminate",
        "token_measurement_valid": valid,
        "measurement_errors": invalid_reasons,
        "usage_record_count": record_count,
        "reported_models": sorted(models),
        "expected_model": expected_model,
        "model_match": models == {expected_model},
        "input_other": totals["inputOther"] if valid else None,
        "input_cache_read": totals["inputCacheRead"] if valid else None,
        "input_cache_creation": totals["inputCacheCreation"] if valid else None,
        "input_context_tokens": input_context if valid else None,
        "output_tokens": output_tokens if valid else None,
        "inference_tokens": input_context + output_tokens if valid else None,
        "fresh_tokens": totals["inputOther"] + totals["inputCacheCreation"] + output_tokens if valid else None,
    }


def extract_usage(
    agent_map_path: Path,
    agents_dir: Path,
    *,
    root: Path = CLOSED_LOOP_ROOT,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    manifest = load_manifest(root)
    mapping = validate_agent_map(load_json(agent_map_path), manifest)
    records: list[dict[str, Any]] = []
    for executor in mapping["executors"]:
        usage = aggregate_wire(agents_dir / executor["agent_id"] / "wire.jsonl", executor["model"])
        records.append({"schema_version": manifest["schema_version"], "role": "executor", **executor, **usage})

    main_record: dict[str, Any] | None = None
    main = mapping.get("main_agent") or mapping.get("planner")
    if main is not None:
        usage = aggregate_wire(agents_dir / main["agent_id"] / "wire.jsonl", main["model"])
        main_record = {
            "schema_version": manifest["schema_version"],
            "role": "main_agent",
            "excluded_from_candidate_ranking": True,
            **main,
            **usage,
        }
    return records, main_record


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract strict candidate usage evidence from agent wire files.")
    parser.add_argument("--agent-map", required=True, type=Path)
    parser.add_argument("--agents-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--main-output", type=Path)
    args = parser.parse_args()
    records, main_record = extract_usage(args.agent_map, args.agents_dir)
    atomic_write_jsonl(args.output, records)
    if args.main_output:
        atomic_write_json(
            args.main_output,
            main_record
            if main_record is not None
            else {"schema_version": load_manifest()["schema_version"], "role": "main_agent", "status": "not_recorded"},
        )
    print(json.dumps({"candidate_usage_records": len(records), "main_agent_excluded": True}, sort_keys=True))


if __name__ == "__main__":
    main()
