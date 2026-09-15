"""CLI orchestration for the external B11 reviewer evaluator.

Official scoring is delegated to the frozen ``short/harness/evaluate.py``.
Every supplemental criterion is then run in its own ``checks.py --check``
subprocess, so one timeout or crash cannot affect the other criteria.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Sequence, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from . import EVALUATOR_VERSION, SCHEMA_VERSION
    from .checks import CHECK_IDS, PERFORMANCE_IDS
except ImportError:  # direct execution by an absolute script path
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from reviewer_evaluator import EVALUATOR_VERSION, SCHEMA_VERSION  # type: ignore
    from reviewer_evaluator.checks import CHECK_IDS, PERFORMANCE_IDS  # type: ignore

SHORT = ROOT / "short"
OFFICIAL = SHORT / "harness" / "evaluate.py"
SUPPLEMENTAL = pathlib.Path(__file__).resolve().with_name("checks.py")
OFFICIAL_TIMEOUT_SECONDS = 180
SUPPLEMENTAL_TIMEOUT_SECONDS = 60
CONDITION = "B"
BINDING_SLOT = "B11"


def _json_failure(error_type: str, message: str) -> dict:
    return {
        "passed": False,
        "status": "evaluator_error",
        "error_type": error_type,
        "message": message[:500],
    }


def _structured_check_failure(check_id: str, error_type: str, message: str) -> dict:
    return {
        "id": check_id,
        "passed": False,
        "duration_ms": 0.0,
        "error_type": error_type,
        "message": message[:300],
    }


def _read_last_json(stdout: str) -> Any:
    lines = stdout.strip().splitlines()
    if not lines:
        raise ValueError("subprocess produced no JSON")
    return json.loads(lines[-1])


def _run_subprocess(
    command: List[str], cwd: pathlib.Path, timeout: float
) -> Tuple[Any, str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"subprocess timed out after {timeout}s") from error
    if completed.returncode != 0:
        detail = completed.stderr[-500:] or completed.stdout[-500:]
        raise RuntimeError(f"subprocess exited {completed.returncode}: {detail}")
    return _read_last_json(completed.stdout), completed.stderr


def _load_object(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_models() -> Dict[str, str]:
    manifest = _load_object(SHORT / "manifest.json")
    return {
        item.get("slot"): item.get("expected_model")
        for item in manifest.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("slot"), str)
    }


def _validate_candidate_id(candidate: str) -> str:
    candidate_id = str(candidate)
    if (
        not candidate_id
        or candidate_id in (".", "..")
        or "/" in candidate_id
        or "\\" in candidate_id
    ):
        raise ValueError("candidate must be a non-path candidate ID")
    return candidate_id


def _resolve_candidate(
    candidate: str, workspace: pathlib.Path
) -> Tuple[pathlib.Path, pathlib.Path, str, dict]:
    """Resolve an explicit ID against a candidate root or explicit parent."""
    candidate_id = _validate_candidate_id(candidate)
    workspace = pathlib.Path(workspace).resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(f"workspace does not exist: {workspace}")

    direct_solution = workspace / "solutions" / "dependency_layers.py"
    child_workspace = workspace / candidate_id
    child_solution = child_workspace / "solutions" / "dependency_layers.py"
    if direct_solution.is_file():
        candidate_workspace = workspace
        solution = direct_solution
    elif child_solution.is_file():
        candidate_workspace = child_workspace
        solution = child_solution
    else:
        raise FileNotFoundError(
            "candidate workspace does not contain solutions/dependency_layers.py: "
            f"{workspace} or {child_workspace}"
        )

    cell = _load_object(candidate_workspace / "cell.json")
    return candidate_workspace, solution, candidate_id, cell


def _identity(
    candidate_id: str,
    cell: dict,
    model_override: str | None = None,
    binding_slot_override: str | None = None,
) -> Tuple[str, str, str | None]:
    """Keep CLI candidate ID authoritative and apply explicit metadata overrides."""
    if binding_slot_override is not None:
        binding_slot = binding_slot_override
    else:
        binding_slot = cell.get("binding_slot") or cell.get("slot") or BINDING_SLOT
    binding_slot = str(binding_slot)
    if model_override is not None:
        model = model_override
    else:
        model = cell.get("expected_model")
        if not isinstance(model, str):
            model = _manifest_models().get(binding_slot)
    return candidate_id, binding_slot, model


def _baseline_hashes() -> dict:
    """Hash only frozen evaluator inputs, excluding all results and candidates."""
    paths = [
        SHORT / "manifest.json",
        SHORT / "harness" / "evaluate.py",
        SHORT / "harness" / "common.py",
        SHORT / "evaluator" / "checks.py",
        SHORT / "evaluator" / "criterion_runner.py",
        SHORT / "evaluator" / "instruction_audit.py",
        SHORT / "validation" / "reference" / "solutions" / "dependency_layers.py",
    ]
    hashes = {}
    for path in paths:
        if path.is_file():
            hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def _usage(official: dict) -> dict:
    usage = official.get("usage") if isinstance(official, dict) else None
    if not isinstance(usage, dict) or not usage.get("available"):
        return {
            "available": False,
            "record_count": 0,
            "source_files": [],
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_tokens": None,
        }
    return usage


def _audit(official: dict) -> dict:
    audit = official.get("audit") if isinstance(official, dict) else None
    return (
        audit
        if isinstance(audit, dict)
        else {"passed": False, "reasons": ["official audit unavailable"]}
    )


def _aggregate_supplemental(
    solution: pathlib.Path, timeout: float | None = None
) -> dict:
    """Run each supplemental criterion in a fresh subprocess and aggregate."""
    criterion_timeout = SUPPLEMENTAL_TIMEOUT_SECONDS if timeout is None else timeout
    results = []
    for check_id in CHECK_IDS:
        try:
            result, _ = _run_subprocess(
                [
                    sys.executable,
                    "-I",
                    str(SUPPLEMENTAL),
                    str(solution),
                    "--check",
                    check_id,
                ],
                ROOT,
                criterion_timeout,
            )
            if not isinstance(result, dict):
                raise ValueError("supplemental criterion returned a non-object")
            if result.get("id") != check_id or not isinstance(
                result.get("passed"), bool
            ):
                raise ValueError("supplemental criterion returned an invalid result")
            duration = result.get("duration_ms")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(float(duration))
                or duration < 0
            ):
                raise ValueError("supplemental criterion returned an invalid duration")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            result = _structured_check_failure(
                check_id, type(error).__name__, str(error)
            )
        results.append(result)

    performance = [
        float(item["duration_ms"])
        for item in results
        if item["id"] in PERFORMANCE_IDS and item["passed"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate": str(pathlib.Path(solution).resolve()),
        "passed": sum(1 for item in results if item["passed"]),
        "total": len(results),
        "checks": results,
        "performance_median_ms": (
            round(statistics.median(performance), 3) if performance else None
        ),
    }


def _all_supplemental_failures(error_type: str, message: str) -> dict:
    results = [
        _structured_check_failure(check_id, error_type, message)
        for check_id in CHECK_IDS
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate": None,
        "passed": 0,
        "total": len(results),
        "checks": results,
        "performance_median_ms": None,
    }


def _default_output(
    workspace: pathlib.Path, candidate_workspace: pathlib.Path | None, run_id: str
) -> pathlib.Path:
    if (
        candidate_workspace is not None
        and workspace.resolve() == candidate_workspace.resolve()
    ):
        return ROOT / "reviewer_evaluator" / f"{run_id}.json"
    return workspace / f"{run_id}.json"


def atomic_json(path: pathlib.Path, value: dict) -> None:
    """Create JSON without replacing an existing output file."""
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


def evaluate(
    run_id: str,
    candidate: str,
    workspace: pathlib.Path,
    *,
    model_override: str | None = None,
    binding_slot_override: str | None = None,
) -> dict:
    """Evaluate one ID, optionally overriding only output identity metadata."""
    workspace = pathlib.Path(workspace).resolve()
    candidate_id = str(candidate)
    if model_override == "" or binding_slot_override == "":
        raise ValueError("model and binding-slot overrides must be non-empty")
    try:
        candidate_workspace, solution, candidate_id, cell = _resolve_candidate(
            candidate, workspace
        )
    except (OSError, ValueError) as error:
        official = _json_failure(type(error).__name__, str(error))
        supplemental = _all_supplemental_failures(
            type(error).__name__, str(error)
        )
        return {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "binding_slot": binding_slot_override,
            "model": model_override,
            "official": official,
            "supplemental": supplemental,
            "audit": _audit(official),
            "usage": _usage(official),
            "evaluator_version": EVALUATOR_VERSION,
            "baseline_hashes": _baseline_hashes(),
        }

    candidate_id, binding_slot, model = _identity(
        candidate_id, cell, model_override, binding_slot_override
    )
    try:
        official, _ = _run_subprocess(
            [
                sys.executable,
                "-I",
                str(OFFICIAL),
                "--workspace",
                str(candidate_workspace),
                "--condition",
                CONDITION,
                "--slot",
                BINDING_SLOT,
            ],
            SHORT / "harness",
            OFFICIAL_TIMEOUT_SECONDS,
        )
        if not isinstance(official, dict):
            raise ValueError("official evaluator returned a non-object")
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        official = _json_failure(type(error).__name__, str(error))

    supplemental = _aggregate_supplemental(solution)
    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "binding_slot": binding_slot,
        "model": model,
        "official": official,
        "supplemental": supplemental,
        "audit": _audit(official),
        "usage": _usage(official),
        "evaluator_version": EVALUATOR_VERSION,
        "baseline_hashes": _baseline_hashes(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="explicit run identifier")
    parser.add_argument("--candidate", required=True, help="explicit candidate ID")
    parser.add_argument(
        "--workspace",
        required=True,
        type=pathlib.Path,
        help="candidate workspace or explicit parent containing the candidate ID",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        help="output JSON path; existing files are never replaced",
    )
    parser.add_argument("--model", help="explicit model metadata override")
    parser.add_argument("--binding-slot", help="explicit binding slot metadata override")
    args = parser.parse_args(argv)
    try:
        workspace = args.workspace.resolve()
        candidate_workspace = None
        try:
            candidate_workspace, _, _, _ = _resolve_candidate(
                args.candidate, workspace
            )
        except (OSError, ValueError):
            pass
        output = (
            args.output.resolve()
            if args.output
            else _default_output(workspace, candidate_workspace, args.run)
        )
        atomic_json(
            output,
            evaluate(
                args.run,
                args.candidate,
                workspace,
                model_override=args.model,
                binding_slot_override=args.binding_slot,
            ),
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        parser.exit(2, f"evaluation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
