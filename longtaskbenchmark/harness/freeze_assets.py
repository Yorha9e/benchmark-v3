from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from common import (
    CLOSED_LOOP_ROOT,
    SchemaError,
    atomic_write_bytes,
    atomic_write_json,
    build_briefing_bytes,
    files_under,
    isolated_python_env,
    load_criteria,
    load_manifest,
    sha256_file,
    utc_now,
    validate_reference_validation_summary,
    validation_expectations,
)


CONTROLLED_DIRECTORIES = ("template", "evaluator", "harness", "validation")
CONTROLLED_FILES = ("manifest.json", "design.md")
VALIDATION_TIMEOUT_SECONDS = 900


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate validation JSON key: {key!r}")
        result[key] = value
    return result


def run_reference_validation(root: Path) -> dict[str, Any]:
    manifest = load_manifest(root)
    criteria_manifest = load_criteria(root, manifest)
    expected_criteria, expected_mutants = validation_expectations(root, criteria_manifest)
    script = root / "validation" / "run_validation.py"
    if not script.is_file():
        raise FileNotFoundError(f"validation runner missing: {script}")
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=root / "validation",
        env=isolated_python_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=VALIDATION_TIMEOUT_SECONDS,
        check=False,
    )
    lines = completed.stdout.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise SchemaError("validation runner must emit exactly one non-empty JSON line")
    try:
        payload = json.loads(
            lines[0],
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SchemaError(f"invalid validation JSON constant: {value}")
            ),
        )
    except json.JSONDecodeError as error:
        raise SchemaError(f"validation runner emitted invalid JSON: {error}") from error
    summary = validate_reference_validation_summary(payload, expected_criteria, expected_mutants)
    if completed.returncode != 0:
        raise RuntimeError(
            f"validation runner returned {completed.returncode}: {completed.stderr[-2000:]}"
        )
    atomic_write_json(root / "validation" / "reference-validation.json", summary)
    return summary


def collect_asset_hashes(root: Path) -> dict[str, str]:
    manifest = load_manifest(root)
    load_criteria(root, manifest)
    hashes: dict[str, str] = {}
    for relative in CONTROLLED_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"controlled asset missing: {path}")
        hashes[relative] = sha256_file(path)
    for directory_name in CONTROLLED_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            raise FileNotFoundError(f"controlled directory missing: {directory}")
        for path in files_under(directory):
            relative = path.relative_to(root).as_posix()
            hashes[relative] = sha256_file(path)
    for relative in (manifest["paths"]["frozen_plan"], manifest["paths"]["briefing"]):
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"controlled asset missing: {path}")
        hashes[relative] = sha256_file(path)
    return dict(sorted(hashes.items()))


def freeze(root: Path = CLOSED_LOOP_ROOT, *, replace: bool = False) -> dict:
    manifest = load_manifest(root)
    output = root / manifest["paths"]["asset_hashes"]
    if output.exists() and not replace:
        raise FileExistsError(f"refusing to overwrite frozen asset manifest: {output}")

    briefing = build_briefing_bytes(root)
    run_reference_validation(root)
    atomic_write_bytes(root / manifest["paths"]["briefing"], briefing)
    atomic_write_bytes(root / "template" / "BRIEFING.md", briefing)
    payload = {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": utc_now(),
        "files": collect_asset_hashes(root),
    }
    atomic_write_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze closed-loop v2 controlled assets.")
    parser.add_argument("--replace", action="store_true", help="explicitly replace an existing asset manifest")
    args = parser.parse_args()
    payload = freeze(replace=args.replace)
    print(f"frozen_assets={len(payload['files'])}")


if __name__ == "__main__":
    main()
