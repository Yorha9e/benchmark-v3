"""Run deterministic reference and mutation validation for closed-loop v2."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


_TEST_RUNNER = r"""
import sys
import unittest

workspace, project_root, target = sys.argv[1:4]
sys.path[:0] = [workspace, project_root]
program = unittest.main(
    module=None,
    argv=["unittest", "-v", target],
    exit=False,
)
raise SystemExit(0 if program.result.wasSuccessful() else 1)
"""
_TEST_COUNT = re.compile(r"Ran\s+(\d+)\s+tests?\b")
_DURATION = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_UNSTABLE_KEYS = frozenset(
    {
        "captured_output",
        "diagnostic",
        "duration_seconds",
        "elapsed",
        "path",
        "timestamp",
        "workspace",
    }
)
_TIMEOUT_SECONDS = 60.0
_ALLOWED_MUTANT_SOURCES = frozenset(
    {
        "src/order_fulfillment/__init__.py",
        "src/order_fulfillment/__main__.py",
        "src/delivery_spool/__init__.py",
        "src/delivery_spool/__main__.py",
    }
)


class ValidationError(RuntimeError):
    """An invalid validation asset or mutation definition."""


def _clean_environment() -> dict[str, str]:
    """Return a small, deterministic environment suitable for isolated Python."""

    allowed = (
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "TZ": "UTC",
        }
    )
    return environment


def _stable_text(text: str, roots: tuple[Path, ...]) -> str:
    """Reduce captured unittest output to deterministic diagnostic text."""

    normalized = text.replace("\\", "/")
    for root in roots:
        root_text = str(root.resolve()).replace("\\", "/")
        normalized = normalized.replace(root_text, "<workspace>")
    normalized = _DURATION.sub("in <duration>", normalized)
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    for line in reversed(lines):
        if line == "OK" or line.startswith("FAILED ("):
            return line
    return lines[-1][:240] if lines else "no unittest output"


def _run_criterion(
    *,
    project_root: Path,
    workspace: Path,
    criterion_id: str,
    target: str,
    environment_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute exactly one official criterion in an isolated subprocess."""

    command = [
        sys.executable,
        "-I",
        "-c",
        _TEST_RUNNER,
        str(workspace),
        str(project_root),
        target,
    ]
    environment = _clean_environment()
    if environment_overrides is not None:
        environment.update(environment_overrides)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = "\n".join(
            part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
            for part in (error.stdout, error.stderr)
        )
        return {
            "criterion": criterion_id,
            "target": target,
            "status": "timeout",
            "passed": False,
            "executed": False,
            "tests_run": 0,
            "returncode": None,
            "duration_seconds": round(time.perf_counter() - started, 6),
            "workspace": str(workspace),
            "captured_output": output,
            "diagnostic": f"safety timeout; {_stable_text(output, (workspace, project_root))}",
        }
    except OSError as error:
        return {
            "criterion": criterion_id,
            "target": target,
            "status": "execution_failure",
            "passed": False,
            "executed": False,
            "tests_run": 0,
            "returncode": None,
            "duration_seconds": round(time.perf_counter() - started, 6),
            "workspace": str(workspace),
            "captured_output": "",
            "diagnostic": f"could not start unittest: {type(error).__name__}",
        }

    output = completed.stdout + "\n" + completed.stderr
    counts = _TEST_COUNT.findall(output)
    tests_run = int(counts[-1]) if counts else 0
    executed = (
        tests_run == 1
        and target in output
        and "_FailedTest" not in output
    )
    passed = executed and completed.returncode == 0
    if passed:
        status = "passed"
    elif executed:
        status = "failed"
    else:
        status = "execution_failure"
    return {
        "criterion": criterion_id,
        "target": target,
        "status": status,
        "passed": passed,
        "executed": executed,
        "tests_run": tests_run,
        "returncode": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "workspace": str(workspace),
        "captured_output": output,
        "diagnostic": _stable_text(output, (workspace, project_root)),
    }


def _load_criteria(criteria_path: Path) -> list[dict[str, str]]:
    """Load and strictly flatten the twenty official criteria."""

    try:
        document = json.loads(criteria_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot load criteria.json: {type(error).__name__}") from error
    if type(document) is not dict or document.get("criterion_count") != 20:
        raise ValidationError("criteria.json must declare criterion_count 20")

    criteria: list[dict[str, str]] = []
    try:
        projects = document["projects"]
        for project in projects:
            for milestone in project["milestones"]:
                for criterion in milestone["criteria"]:
                    criterion_id = criterion["id"]
                    target = criterion["unittest"]
                    if type(criterion_id) is not str or type(target) is not str:
                        raise TypeError("criterion id and unittest must be strings")
                    criteria.append({"id": criterion_id, "unittest": target})
    except (KeyError, TypeError) as error:
        raise ValidationError("criteria.json has an invalid criterion structure") from error

    ids = [criterion["id"] for criterion in criteria]
    targets = [criterion["unittest"] for criterion in criteria]
    if len(criteria) != 20 or len(set(ids)) != 20 or len(set(targets)) != 20:
        raise ValidationError("criteria.json must contain 20 unique ids and unittests")
    return criteria


def _normalize(value: Any) -> Any:
    """Remove timing, path, and diagnostic instability before run comparison."""

    if type(value) is dict:
        return {
            key: _normalize(item)
            for key, item in sorted(value.items())
            if key not in _UNSTABLE_KEYS
        }
    if type(value) is list:
        return [_normalize(item) for item in value]
    return value


def _reference_run(
    *, project_root: Path, reference: Path, criteria: list[dict[str, str]]
) -> list[dict[str, Any]]:
    return [
        _run_criterion(
            project_root=project_root,
            workspace=reference,
            criterion_id=criterion["id"],
            target=criterion["unittest"],
        )
        for criterion in criteria
    ]


def _load_mutants(
    manifest_path: Path, criteria_by_id: dict[str, str]
) -> list[dict[str, Any]]:
    """Load mutation definitions and bind each to one exact criterion."""

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot load mutant manifest: {type(error).__name__}") from error
    if type(document) is not dict or set(document) != {"schema_version", "mutants"}:
        raise ValidationError("mutant manifest has invalid top-level fields")
    if document["schema_version"] != 1 or type(document["mutants"]) is not list:
        raise ValidationError("mutant manifest schema is unsupported")

    mutants = document["mutants"]
    if len(mutants) < 10:
        raise ValidationError("at least 10 targeted mutants are required")
    seen: set[str] = set()
    for mutant in mutants:
        if type(mutant) is not dict or set(mutant) != {
            "id",
            "target_criterion",
            "source",
            "replacements",
        }:
            raise ValidationError("each mutant must declare exact required fields")
        mutant_id = mutant["id"]
        criterion_id = mutant["target_criterion"]
        source = mutant["source"]
        replacements = mutant["replacements"]
        if type(mutant_id) is not str or not mutant_id or mutant_id in seen:
            raise ValidationError("mutant ids must be unique non-empty strings")
        seen.add(mutant_id)
        if type(criterion_id) is not str or criterion_id not in criteria_by_id:
            raise ValidationError(f"mutant {mutant_id} targets an unknown criterion")
        if type(source) is not str or source not in _ALLOWED_MUTANT_SOURCES:
            raise ValidationError(f"mutant {mutant_id} has an invalid source path")
        if type(replacements) is not list or not replacements:
            raise ValidationError(f"mutant {mutant_id} has no source replacements")
        for replacement in replacements:
            if type(replacement) is not dict or set(replacement) != {"old", "new"}:
                raise ValidationError(f"mutant {mutant_id} has an invalid replacement")
            old, new = replacement["old"], replacement["new"]
            if type(old) is not str or type(new) is not str or not old or old == new:
                raise ValidationError(f"mutant {mutant_id} has an invalid replacement value")
    return mutants


def _apply_mutant(mutant: dict[str, Any], workspace: Path) -> None:
    """Apply replacements, requiring each current anchor exactly once."""

    source_path = workspace / mutant["source"]
    try:
        contents = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(
            f"mutant {mutant['id']} cannot read copied reference source"
        ) from error
    for index, replacement in enumerate(mutant["replacements"], start=1):
        count = contents.count(replacement["old"])
        if count != 1:
            raise ValidationError(
                f"mutant {mutant['id']} replacement {index} anchor count is {count}, expected 1"
            )
        contents = contents.replace(replacement["old"], replacement["new"], 1)
    try:
        compile(contents, str(source_path), "exec")
        source_path.write_text(contents, encoding="utf-8", newline="\n")
    except (OSError, UnicodeError, SyntaxError) as error:
        raise ValidationError(
            f"mutant {mutant['id']} did not produce executable Python: {type(error).__name__}"
        ) from error


def _claim_concurrency_oracle(
    marker_root: Path, raw_output: str, *, parties: int
) -> tuple[bool, str]:
    """Prove that unlocked claimers read one state and became winners."""

    ready = sorted(marker_root.glob("ready-*"))
    winner_paths = sorted(marker_root.glob("winner-*"))
    message_ids: list[str] = []
    attempts: list[int] = []
    malformed = 0
    for path in winner_paths:
        try:
            message_id, separator, attempt_text = path.read_text(
                encoding="utf-8"
            ).partition(":")
            if separator != ":" or not attempt_text.isdigit():
                malformed += 1
                continue
            message_ids.append(message_id)
            attempts.append(int(attempt_text))
        except (OSError, UnicodeError):
            malformed += 1

    assertion = re.search(r"AssertionError:\s*(\d+)\s*!=\s*1\b", raw_output)
    asserted_winners = int(assertion.group(1)) if assertion is not None else None
    oracle_passed = (
        len(ready) == parties
        and len(winner_paths) == parties
        and malformed == 0
        and set(message_ids) == {"only-message"}
        and attempts == [1] * parties
        and asserted_winners == parties
    )
    outcome = (
        "multiple winners with lost attempt updates"
        if oracle_passed
        else "concurrency oracle not satisfied"
    )
    diagnostic = (
        f"concurrency oracle: ready={len(ready)}/{parties}, "
        f"winners={len(winner_paths)}, asserted_winners={asserted_winners}, "
        f"message_ids={sorted(set(message_ids))}, attempts={attempts}; {outcome}"
    )
    return oracle_passed, diagnostic


def _run_mutants(
    *,
    project_root: Path,
    reference: Path,
    mutants_root: Path,
    criteria_by_id: dict[str, str],
    mutants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy reference source, apply each mutant, and run its official criterion."""

    results: list[dict[str, Any]] = []
    for mutant in mutants:
        criterion_id = mutant["target_criterion"]
        target = criteria_by_id[criterion_id]
        with tempfile.TemporaryDirectory(
            prefix=f".{mutant['id']}-", dir=mutants_root
        ) as temporary:
            workspace = Path(temporary)
            shutil.copytree(reference / "src", workspace / "src")
            try:
                _apply_mutant(mutant, workspace)
            except ValidationError as error:
                results.append(
                    {
                        "id": mutant["id"],
                        "criterion": criterion_id,
                        "detected": False,
                        "status": "mutation_failure",
                        "diagnostic": str(error),
                    }
                )
                continue

            environment_overrides = None
            rendezvous_root = workspace / ".claim-rendezvous"
            if mutant["id"] == "spool_no_lock_double_claim":
                rendezvous_root.mkdir()
                environment_overrides = {
                    "CLOSED_LOOP_CLAIM_PARTIES": "6",
                    "CLOSED_LOOP_CLAIM_RENDEZVOUS": str(rendezvous_root),
                }
            raw = _run_criterion(
                project_root=project_root,
                workspace=workspace,
                criterion_id=criterion_id,
                target=target,
                environment_overrides=environment_overrides,
            )

            if mutant["id"] == "spool_no_lock_double_claim":
                oracle_passed, diagnostic = _claim_concurrency_oracle(
                    rendezvous_root,
                    raw["captured_output"],
                    parties=6,
                )
                detected = raw["status"] == "failed" and oracle_passed
                status = (
                    "detected"
                    if detected
                    else "oracle_failure" if raw["status"] == "failed" else raw["status"]
                )
            else:
                detected = raw["status"] == "failed"
                status = "detected" if detected else raw["status"]
                diagnostic = (
                    f"Ran {raw['tests_run']} test; {raw['diagnostic']}"
                    if raw["executed"]
                    else raw["diagnostic"]
                )
            results.append(
                {
                    "id": mutant["id"],
                    "criterion": criterion_id,
                    "detected": detected,
                    "status": status,
                    "diagnostic": diagnostic,
                }
            )
    return results


def _empty_summary() -> dict[str, Any]:
    return {
        "reference_runs": [],
        "normalized_consistent": False,
        "criteria": {"passed": 0, "total": 20},
        "reference_score": "0/20",
        "mutants": [],
        "overall_passed": False,
    }


def _execute() -> dict[str, Any]:
    validation_root = Path(__file__).resolve().parent
    suite_root = validation_root.parent
    # The reusable copy is self-contained; evaluator test targets import the
    # compatibility namespace under this benchmark root.
    project_root = suite_root
    criteria_path = suite_root / "evaluator" / "criteria.json"
    reference = validation_root / "reference"
    mutants_root = validation_root / "mutants"
    manifest_path = mutants_root / "manifest.json"

    criteria = _load_criteria(criteria_path)
    criteria_by_id = {
        criterion["id"]: criterion["unittest"] for criterion in criteria
    }
    if not (reference / "src" / "order_fulfillment" / "__init__.py").is_file():
        raise ValidationError("order reference package is missing")
    if not (reference / "src" / "delivery_spool" / "__init__.py").is_file():
        raise ValidationError("spool reference package is missing")
    mutants = _load_mutants(manifest_path, criteria_by_id)

    raw_runs = [
        _reference_run(
            project_root=project_root,
            reference=reference,
            criteria=criteria,
        )
        for _ in range(2)
    ]
    normalized_runs = [_normalize(run) for run in raw_runs]
    normalized_consistent = normalized_runs[0] == normalized_runs[1]
    reference_runs = []
    for index, run in enumerate(normalized_runs, start=1):
        passed = sum(result["passed"] for result in run)
        reference_runs.append(
            {
                "run": index,
                "passed": passed,
                "total": len(run),
                "criteria": run,
            }
        )

    mutant_results = _run_mutants(
        project_root=project_root,
        reference=reference,
        mutants_root=mutants_root,
        criteria_by_id=criteria_by_id,
        mutants=mutants,
    )
    reference_passed = min(run["passed"] for run in reference_runs)
    all_reference_passed = all(run["passed"] == 20 for run in reference_runs)
    all_mutants_detected = (
        len(mutant_results) >= 10
        and all(result["detected"] for result in mutant_results)
    )
    overall_passed = (
        len(criteria) == 20
        and all_reference_passed
        and normalized_consistent
        and all_mutants_detected
    )
    return {
        "reference_runs": reference_runs,
        "normalized_consistent": normalized_consistent,
        "criteria": {"passed": reference_passed, "total": 20},
        "reference_score": f"{reference_passed}/20",
        "mutants": mutant_results,
        "overall_passed": overall_passed,
    }


def main() -> None:
    summary = _empty_summary()
    try:
        summary = _execute()
    except Exception as error:
        summary["validation_error"] = f"{type(error).__name__}: {error}"
    encoded = json.dumps(
        summary,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    sys.stdout.write(encoded + "\n")
    raise SystemExit(0 if summary.get("overall_passed") is True else 1)


if __name__ == "__main__":
    main()
