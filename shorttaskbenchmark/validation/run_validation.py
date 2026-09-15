"""Validate the evaluator against a reference and criterion-directed mutants."""

import contextlib
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "harness"
EVALUATOR = ROOT / "evaluator"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
if str(EVALUATOR) not in sys.path:
    sys.path.insert(0, str(EVALUATOR))
from common import load_manifest
from evaluate import evaluate
from instruction_audit import audit_workspace


MUTANTS = {
    "rp_delete_identity": ("rp_delete_identity.py", "rp_delete_identity"),
    "rp_plain_dict": ("rp_plain_dict.py", "rp_plain_dict"),
    "rp_isolation": ("rp_isolation.py", "rp_isolation"),
    "rp_order_boundary": ("rp_order_boundary.py", "rp_order_boundary"),
    "dl_one_shot": ("dl_one_shot.py", "dl_one_shot"),
    "dl_dependency_nodes": ("dl_dependency_nodes.py", "dl_dependency_nodes"),
    "dl_stable_order_cycle": ("dl_stable_order_cycle.py", "dl_stable_order_cycle"),
    "dl_deep_iterative": ("dl_deep_iterative.py", "dl_deep_iterative"),
    "ttl_strict_types": ("ttl_strict_types.py", "ttl_strict_types"),
    "ttl_exact_expiry": ("ttl_exact_expiry.py", "ttl_exact_expiry"),
    "ttl_capacity": ("ttl_capacity.py", "ttl_capacity"),
    "ttl_gc_release": ("ttl_gc_release.py", "ttl_gc_release"),
    "du_strict_syntax": ("du_strict_syntax.py", "du_strict_syntax"),
    "du_units_ranges": ("du_units_ranges.py", "du_units_ranges"),
    "du_normalize": ("du_normalize.py", "du_normalize"),
    "du_structured_errors": ("du_structured_errors.py", "du_structured_errors"),
}


def _run_criterion(criterion, solution, timeout=10):
    command = [sys.executable, "-I", str(EVALUATOR / "criterion_runner.py"), criterion, str(solution)]
    completed = subprocess.run(command, cwd=str(solution.parent.parent), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise AssertionError(f"criterion runner failed for {criterion}: {completed.stderr[-500:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _reference_workspace():
    return ROOT / "validation" / "reference"


def _assert_reference(manifest):
    first = evaluate(ROOT, "A", "reference")
    second = evaluate(ROOT, "A", "reference")
    first_normalized = json.dumps(first, sort_keys=True, separators=(",", ":"))
    second_normalized = json.dumps(second, sort_keys=True, separators=(",", ":"))
    assert first_normalized == second_normalized, "reference evaluation is not deterministic"
    assert first["instruction_gate"] is True
    assert first["strict"]["OfficialCriterionCount"] == sum(len(task["criteria"]) for task in manifest["tasks"])
    assert first["strict"]["StrictTaskCount"] == len(manifest["tasks"])
    for kind, values in first["capabilities"].items():
        assert all(item["passed"] for item in values), f"reference capability failed: {kind}"
    return first


def _assert_mutants():
    for criterion, (filename, expected) in MUTANTS.items():
        result = _run_criterion(criterion, ROOT / "validation" / "mutants" / filename)
        assert result.get("passed") is False, f"mutant was not caught: {criterion}"


def _copy_candidate(manifest, directory):
    workspace = pathlib.Path(directory) / "candidate"
    shutil.copytree(ROOT / "template", workspace)
    shutil.copy2(ROOT / "TASKS.md", workspace / "TASKS.md")
    reference = _reference_workspace()
    for task in manifest["tasks"]:
        shutil.copy2(reference / "solutions" / task["file"], workspace / "solutions" / task["file"])
    shutil.copy2(reference / "instruction_ack.json", workspace / "instruction_ack.json")
    cell = {"benchmark_id": manifest["benchmark_id"], "condition": "A", "slot": "A01", "expected_model": "MT/LongCat-2.0", "group": "G1", "wave": "G1-A", "public_inputs": ["TASKS.md", "public_smoke_tests.py"]}
    (workspace / "cell.json").write_text(json.dumps(cell), encoding="utf-8")
    return workspace


def _assert_gate_tampering(manifest):
    with tempfile.TemporaryDirectory(prefix="short-validation-", dir=str(ROOT / "runs")) as temporary:
        workspace = _copy_candidate(manifest, temporary)
        assert audit_workspace(workspace, manifest, ROOT)["passed"]

        smoke = workspace / "public_smoke_tests.py"
        original_smoke = smoke.read_text(encoding="utf-8")
        smoke.write_text(original_smoke + "\n# tampered\n", encoding="utf-8")
        assert not audit_workspace(workspace, manifest, ROOT)["passed"]
        smoke.write_text(original_smoke, encoding="utf-8")

        solution = workspace / "solutions" / "recursive_patch.py"
        original_solution = solution.read_text(encoding="utf-8")
        solution.write_text("import definitely_not_a_standard_library\n" + original_solution, encoding="utf-8")
        assert not audit_workspace(workspace, manifest, ROOT)["passed"]
        solution.write_text(original_solution, encoding="utf-8")

        solution.write_text(original_solution.replace("def apply_patch(base, patch, delete=DELETE):", "def apply_patch(base, patch):"), encoding="utf-8")
        assert not audit_workspace(workspace, manifest, ROOT)["passed"]
        solution.write_text(original_solution, encoding="utf-8")

        (workspace / "unexpected.txt").write_text("outside contract", encoding="utf-8")
        assert not audit_workspace(workspace, manifest, ROOT)["passed"]
        assert not audit_workspace(workspace, manifest, ROOT, external_changes=("external.txt",))["passed"]


def main():
    manifest = load_manifest(ROOT)
    reference = _assert_reference(manifest)
    _assert_mutants()
    _assert_gate_tampering(manifest)
    result = {
        "benchmark_id": manifest["benchmark_id"],
        "reference": {"strict_tasks": reference["strict"]["StrictTaskCount"], "official_criteria": reference["strict"]["OfficialCriterionCount"]},
        "mutants_caught": len(MUTANTS),
        "gate_tamper_checks": 5,
        "status": manifest.get("status", {}),
    }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
