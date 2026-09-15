from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from common import (
    CLOSED_LOOP_ROOT,
    atomic_write_json,
    atomic_write_jsonl,
    build_briefing_bytes,
    expected_model,
    load_criteria,
    load_json,
    load_jsonl,
    load_manifest,
    ordered_slots,
    sha256_bytes,
    tree_snapshot,
    utc_now,
    validate_reference_validation_summary,
    validation_expectations,
    verify_assets,
    wave_for_slot,
)


EXPECTED_STATUS = "expected"


def dispatch_order(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"sequence": index, "wave": wave, "slots": list(wave_slots)}
        for index, (wave, wave_slots) in enumerate(manifest["waves"].items(), start=1)
    ]


def _preflight(
    root: Path, manifest: dict[str, Any], criteria_manifest: dict[str, Any]
) -> tuple[bytes, list[str]]:
    plan = root / manifest["paths"]["frozen_plan"]
    if not plan.is_file():
        raise FileNotFoundError(f"frozen plan missing; no workspace created: {plan}")
    validation_path = root / "validation" / "reference-validation.json"
    if not validation_path.is_file():
        raise FileNotFoundError(f"frozen reference validation missing: {validation_path}")
    expected_criteria, expected_mutants = validation_expectations(root, criteria_manifest)
    validate_reference_validation_summary(
        load_json(validation_path), expected_criteria, expected_mutants
    )

    briefing = build_briefing_bytes(root)
    violations = verify_assets(root)
    if violations:
        raise RuntimeError("asset verification failed: " + "; ".join(violations))
    briefing_targets = [root / manifest["paths"]["briefing"], root / "template" / "BRIEFING.md"]
    invalid_briefings = [
        path for path in briefing_targets if not path.is_file() or path.read_bytes() != briefing
    ]
    if invalid_briefings:
        raise RuntimeError(
            "frozen briefing missing or mismatched: " + ", ".join(str(path) for path in invalid_briefings)
        )

    slots = ordered_slots(manifest)
    runs_root = root / "runs"
    existing_workspaces = [
        runs_root / slot / "workspace"
        for slot in slots
        if (runs_root / slot / "workspace").exists()
    ]
    if existing_workspaces:
        raise FileExistsError(
            "refusing to overwrite existing workspace(s): "
            + ", ".join(str(path) for path in existing_workspaces)
        )
    results = root / "results"
    result_targets = [
        results / "expected-cells.jsonl",
        results / "prepared-workspaces.json",
        results / "assignments.json",
    ]
    existing_results = [path for path in result_targets if path.exists()]
    if existing_results:
        raise FileExistsError(
            "refusing to overwrite preparation result(s): "
            + ", ".join(str(path) for path in existing_results)
        )
    return briefing, slots


def prepare(root: Path = CLOSED_LOOP_ROOT) -> dict[str, Any]:
    manifest = load_manifest(root)
    criteria = load_criteria(root, manifest)
    briefing, slots = _preflight(root, manifest, criteria)
    briefing_digest = sha256_bytes(briefing)
    template = root / "template"
    runs_root = root / "runs"
    results_root = root / "results"

    staging_parent = Path(tempfile.mkdtemp(prefix=".prepare-runs-", dir=root))
    staged_workspaces: dict[str, Path] = {}
    published_workspaces: list[Path] = []
    published_results: list[Path] = []
    created_slot_directories: list[Path] = []
    try:
        for slot in slots:
            staged = staging_parent / "workspaces" / slot / "workspace"
            shutil.copytree(template, staged)
            workspace_briefing = staged / "BRIEFING.md"
            if not workspace_briefing.is_file() or workspace_briefing.read_bytes() != briefing:
                raise RuntimeError(f"staged briefing hash mismatch for {slot}")
            staged_workspaces[slot] = staged

        created_at = utc_now()
        cells: list[dict[str, Any]] = []
        for slot in slots:
            snapshot = tree_snapshot(staged_workspaces[slot])
            cells.append(
                {
                    "schema_version": manifest["schema_version"],
                    "benchmark_version": manifest["benchmark_version"],
                    "expected_cell_id": f"{manifest['benchmark_version']}-{slot}-pass1",
                    "slot": slot,
                    "expected_model": expected_model(manifest, slot),
                    "thinking_effort": manifest["candidate_slots"][slot].get("thinking_effort"),
                    "wave": wave_for_slot(manifest, slot),
                    "workspace": (runs_root / slot / "workspace").relative_to(root).as_posix(),
                    "workspace_before": snapshot,
                    "briefing_sha256": briefing_digest,
                    "status": EXPECTED_STATUS,
                    "created_at": created_at,
                }
            )

        ordered_dispatch = dispatch_order(manifest)
        assignments = {
            "schema_version": manifest["schema_version"],
            "benchmark_version": manifest["benchmark_version"],
            "task_spec": {
                "path": "template/TASKS.md",
                "sha256": sha256_bytes((template / "TASKS.md").read_bytes()),
            },
            "frozen_plan": {
                "path": manifest["paths"]["frozen_plan"],
                "sha256": sha256_bytes((root / manifest["paths"]["frozen_plan"]).read_bytes()),
            },
            "briefing": {"path": manifest["paths"]["briefing"], "sha256": briefing_digest},
            "waves": manifest["waves"],
            "dispatch_order": ordered_dispatch,
            "criteria_count": criteria["criterion_count"],
            "milestone_count": criteria["milestone_count"],
            "slots": cells,
        }
        prepared = {
            "schema_version": manifest["schema_version"],
            "benchmark_version": manifest["benchmark_version"],
            "briefing_sha256": briefing_digest,
            "cells": cells,
        }
        staged_results = staging_parent / "results"
        atomic_write_jsonl(staged_results / "expected-cells.jsonl", cells)
        atomic_write_json(staged_results / "prepared-workspaces.json", prepared)
        atomic_write_json(staged_results / "assignments.json", assignments)

        if load_jsonl(staged_results / "expected-cells.jsonl") != cells:
            raise RuntimeError("staged expected-cells validation failed")
        if load_json(staged_results / "prepared-workspaces.json") != prepared:
            raise RuntimeError("staged prepared-workspaces validation failed")
        if load_json(staged_results / "assignments.json") != assignments:
            raise RuntimeError("staged assignments validation failed")
        for slot in slots:
            if tree_snapshot(staged_workspaces[slot]) != cells[slots.index(slot)]["workspace_before"]:
                raise RuntimeError(f"staged workspace changed before publication for {slot}")

        results_root.mkdir(parents=True, exist_ok=True)
        for name in ("expected-cells.jsonl", "prepared-workspaces.json", "assignments.json"):
            destination = results_root / name
            if destination.exists():
                raise FileExistsError(f"preparation result appeared during publication: {destination}")
            os.replace(staged_results / name, destination)
            published_results.append(destination)
        for slot in slots:
            destination = runs_root / slot / "workspace"
            if not destination.parent.exists():
                destination.parent.mkdir(parents=True)
                created_slot_directories.append(destination.parent)
            if destination.exists():
                raise FileExistsError(f"workspace appeared during publication: {destination}")
            os.replace(staged_workspaces[slot], destination)
            published_workspaces.append(destination)
            if (destination / "BRIEFING.md").read_bytes() != briefing:
                raise RuntimeError(f"published briefing hash mismatch for {slot}")
        shutil.rmtree(staging_parent)
        return prepared
    except BaseException as error:
        cleanup_errors: list[str] = []
        for workspace in reversed(published_workspaces):
            try:
                shutil.rmtree(workspace)
            except OSError as cleanup_error:
                cleanup_errors.append(f"cannot remove {workspace}: {cleanup_error}")
        for result in reversed(published_results):
            try:
                result.unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup_errors.append(f"cannot remove {result}: {cleanup_error}")
        for directory in reversed(created_slot_directories):
            try:
                directory.rmdir()
            except OSError as cleanup_error:
                cleanup_errors.append(f"cannot remove {directory}: {cleanup_error}")
        try:
            runs_root.rmdir()
        except OSError as cleanup_error:
            if runs_root.exists() and not any(runs_root.iterdir()):
                cleanup_errors.append(f"cannot remove empty {runs_root}: {cleanup_error}")
        if staging_parent.exists():
            try:
                shutil.rmtree(staging_parent)
            except OSError as cleanup_error:
                cleanup_errors.append(f"cannot remove staging {staging_parent}: {cleanup_error}")
        if cleanup_errors:
            error.add_note("prepare rollback issues: " + "; ".join(cleanup_errors))
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Create one closed-loop v2 workspace per manifest slot.")
    parser.parse_args()
    payload = prepare()
    print(json.dumps({"prepared_workspaces": len(payload["cells"]), "briefing_sha256": payload["briefing_sha256"]}, sort_keys=True))


if __name__ == "__main__":
    main()
