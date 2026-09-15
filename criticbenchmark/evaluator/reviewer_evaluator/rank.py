"""Strict and lenient deterministic rankings for explicit reviewer run JSON.

Only an explicitly supplied JSON file or run directory is read.  Human/LLM
reviews are advisory and never rewrite either deterministic board.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import statistics
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple

PERFORMANCE_IDS = frozenset(("deep_50000", "wide_50000"))


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _metrics(run: dict) -> dict:
    official = _as_dict(run.get("official"))
    strict = _as_dict(official.get("strict"))
    lenient = _as_dict(official.get("lenient"))
    capabilities = _as_dict(official.get("capabilities"))

    resource_values = capabilities.get("resource")
    if isinstance(resource_values, list):
        resource_count = sum(
            bool(_as_dict(item).get("passed")) for item in resource_values
        )
    else:
        resource_count = int(strict.get("ResourceCapabilityCount", 0) or 0)

    gate = int(
        bool(strict.get("InstructionGate", official.get("instruction_gate", False)))
    )
    raw = lenient.get("RawCriterionCount")
    if not isinstance(raw, int) or isinstance(raw, bool):
        criteria = official.get("criteria")
        raw = (
            sum(bool(_as_dict(item).get("passed")) for item in criteria)
            if isinstance(criteria, list)
            else 0
        )

    supplemental = _as_dict(run.get("supplemental"))
    checks = supplemental.get("checks")
    passed = supplemental.get("passed")
    if not isinstance(passed, int) or isinstance(passed, bool):
        passed = (
            sum(bool(_as_dict(item).get("passed")) for item in checks)
            if isinstance(checks, list)
            else 0
        )

    performance = supplemental.get("performance_median_ms")
    if not isinstance(performance, (int, float)) or isinstance(performance, bool):
        measurements = [
            _as_dict(item).get("duration_ms")
            for item in (checks if isinstance(checks, list) else [])
            if _as_dict(item).get("id") in PERFORMANCE_IDS
            and _as_dict(item).get("passed")
        ]
        measurements = [
            item
            for item in measurements
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
        performance = statistics.median(measurements) if measurements else None
    performance_missing = (
        performance is None
        or not isinstance(performance, (int, float))
        or isinstance(performance, bool)
        or not math.isfinite(float(performance))
    )

    usage = _as_dict(run.get("usage"))
    token_value = usage.get("total_tokens") if usage.get("available") else None
    token_missing = (
        not isinstance(token_value, (int, float))
        or isinstance(token_value, bool)
        or not math.isfinite(float(token_value))
    )
    return {
        "InstructionGate": gate,
        "official_raw_criterion_count": int(raw),
        "resource_capability_count": int(resource_count),
        "supplemental_passed_count": int(passed),
        "supplemental_performance_median_ms": (
            None if performance_missing else float(performance)
        ),
        "token_usage": None if token_missing else float(token_value),
    }


def _strict_key(metrics: dict) -> Tuple[Any, ...]:
    return (
        -metrics["InstructionGate"],
        -metrics["official_raw_criterion_count"],
        -metrics["resource_capability_count"],
        -metrics["supplemental_passed_count"],
        metrics["supplemental_performance_median_ms"]
        if metrics["supplemental_performance_median_ms"] is not None
        else math.inf,
        metrics["token_usage"] if metrics["token_usage"] is not None else math.inf,
    )


def _lenient_key(metrics: dict) -> Tuple[Any, ...]:
    return (
        -metrics["official_raw_criterion_count"],
        -metrics["resource_capability_count"],
        -metrics["supplemental_passed_count"],
        metrics["supplemental_performance_median_ms"]
        if metrics["supplemental_performance_median_ms"] is not None
        else math.inf,
        metrics["token_usage"] if metrics["token_usage"] is not None else math.inf,
    )


def _board(
    entries: List[Tuple[str, dict]],
    key_function: Callable[[dict], Tuple[Any, ...]],
) -> Tuple[List[dict], List[List[str]]]:
    ordered = sorted(
        ((key_function(metrics), candidate_id, metrics) for candidate_id, metrics in entries),
        key=lambda item: (item[0], item[1]),
    )
    ranking = []
    tie_groups: Dict[Tuple[Any, ...], List[str]] = {}
    previous_key = None
    current_rank = 0
    for index, (key, candidate_id, metrics) in enumerate(ordered, 1):
        if previous_key is None or key != previous_key:
            current_rank = index
            previous_key = key
        ranking.append(
            {"rank": current_rank, "candidate_id": candidate_id, "score": metrics}
        )
        tie_groups.setdefault(key, []).append(candidate_id)
    unresolved = [sorted(ids) for ids in tie_groups.values() if len(ids) > 1]
    unresolved.sort(key=lambda ids: ids[0])
    return ranking, unresolved


def _load_runs(path: pathlib.Path) -> List[dict]:
    """Load one explicit JSON file or direct JSON children of one explicit dir."""
    path = pathlib.Path(path).resolve()
    if path.is_file():
        values = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(values, dict) and isinstance(values.get("runs"), list):
            values = values["runs"]
        elif isinstance(values, dict):
            values = [values]
        if not isinstance(values, list) or not all(
            isinstance(item, dict) for item in values
        ):
            raise ValueError("input JSON must be a run object or list of run objects")
        return list(values)
    if not path.is_dir():
        raise FileNotFoundError(f"explicit ranking input does not exist: {path}")
    runs = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if child.is_file() and child.suffix.lower() == ".json":
            value = json.loads(child.read_text(encoding="utf-8"))
            if isinstance(value, dict) and "candidate_id" in value:
                runs.append(value)
    if not runs:
        raise ValueError(f"explicit run directory has no run JSON files: {path}")
    return runs


def rank(runs: Iterable[dict]) -> dict:
    """Return strict/lenient boards with true tied ranks and tie groups."""
    entries = []
    seen = set()
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("every run must be an object")
        candidate_id = str(run.get("candidate_id", ""))
        if not candidate_id:
            raise ValueError("every run must have a non-empty candidate_id")
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        seen.add(candidate_id)
        entries.append((candidate_id, _metrics(run)))

    strict_ranking, strict_ties = _board(entries, _strict_key)
    lenient_ranking, lenient_ties = _board(entries, _lenient_key)
    return {
        "strict_deterministic_rank": strict_ranking,
        "lenient_deterministic_rank": lenient_ranking,
        "unresolved_ties": {"strict": strict_ties, "lenient": lenient_ties},
        "tie_break_note": (
            "Candidate IDs only stabilize display order; equal board keys share "
            "the same rank and remain unresolved."
        ),
    }


def atomic_json(path: pathlib.Path, value: dict) -> None:
    """Write a ranking report atomically and refuse to replace a file."""
    path = pathlib.Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise FileExistsError(f"refusing to overwrite {path}") from None
        except OSError:
            if path.exists():
                raise FileExistsError(f"refusing to overwrite {path}") from None
            os.rename(temporary, path)
        else:
            temporary.unlink()
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", nargs="?", type=pathlib.Path, help="explicit run JSON or run directory"
    )
    parser.add_argument(
        "--input", dest="input_option", type=pathlib.Path, help="explicit run JSON file"
    )
    parser.add_argument("--run-dir", type=pathlib.Path, help="explicit run directory")
    parser.add_argument(
        "--output", type=pathlib.Path,
        help="ranking JSON output; existing files are never replaced",
    )
    args = parser.parse_args(argv)
    choices = [
        item for item in (args.input, args.input_option, args.run_dir) if item is not None
    ]
    if len(choices) != 1:
        parser.error("provide exactly one explicit JSON file or run directory")
    try:
        result = rank(_load_runs(choices[0]))
        if args.output:
            atomic_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(2, f"ranking failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
