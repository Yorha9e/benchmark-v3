"""Evaluate one candidate cell with isolated criterion subprocesses."""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from common import atomic_json, benchmark_root, candidate_for, changed_paths, load_manifest, read_usage, tree_snapshot


def _resolve_root(workspace):
    workspace = pathlib.Path(workspace).resolve()
    if (workspace / "manifest.json").is_file():
        return workspace
    return benchmark_root()


def _slot_dir(root, requested_workspace, condition, slot):
    requested_workspace = pathlib.Path(requested_workspace).resolve()
    if (requested_workspace / "solutions").is_dir():
        return requested_workspace
    if slot == "reference":
        return root / "validation" / "reference"
    candidates = (
        requested_workspace / condition / slot,
        requested_workspace / "prepared" / condition / slot,
        requested_workspace / "runs" / "prepared" / condition / slot,
        root / condition / slot,
        root / "runs" / "prepared" / condition / slot,
        root / "runs" / condition / slot,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _external_changes(root, requested_workspace, slot_dir, condition, slot, run_roots=()):
    containers = [pathlib.Path(requested_workspace).resolve(), root / "runs" / "prepared"]
    for container in containers:
        ledger_path = container / "_tree-baseline.json"
        if not ledger_path.is_file():
            continue
        try:
            relative_slot = slot_dir.resolve().relative_to(container).as_posix().rstrip("/") + "/"
        except ValueError:
            continue
        try:
            with ledger_path.open("r", encoding="utf-8") as handle:
                ledger = json.load(handle)
            baseline = ledger["tree"]
            expected_digest = hashlib.sha256(json.dumps({"benchmark_id": ledger["benchmark_id"], "tree": baseline}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if ledger.get("digest") != expected_digest:
                return ["_tree-baseline.json"]
            current = tree_snapshot(container)
        except (OSError, KeyError, UnicodeError):
            return ["_tree-baseline.json"]
        changed = []
        for path in changed_paths(baseline, current):
            first_component = path.split("/", 1)[0]
            if path == "_tree-baseline.json" or path.startswith(relative_slot) or first_component in set(run_roots):
                continue
            changed.append(path)
        return changed
    return []


def _criterion(runner, criterion, solution, timeout, max_output_bytes):
    command = [sys.executable, "-I", str(runner), criterion, str(solution)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = str(solution.parent)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(solution.parent.parent),
            env=env,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {"criterion": criterion, "passed": False, "status": "timeout"}
    except OSError as error:
        return {"criterion": criterion, "passed": False, "status": "runner_error", "message": str(error)[:500]}
    if len(stdout.encode("utf-8", "replace")) + len(stderr.encode("utf-8", "replace")) > max_output_bytes:
        return {"criterion": criterion, "passed": False, "status": "output_limit"}
    lines = stdout.strip().splitlines()
    if process.returncode != 0 or not lines:
        return {"criterion": criterion, "passed": False, "status": "runner_error", "message": stderr[-500:]}
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return {"criterion": criterion, "passed": False, "status": "runner_error", "message": lines[-1][:500]}
    if result.get("criterion") != criterion or not isinstance(result.get("passed"), bool):
        return {"criterion": criterion, "passed": False, "status": "runner_error"}
    if result.get("error_type") == "_OutputLimit":
        result["status"] = "output_limit"
    return result


def _cell_metadata(slot_dir):
    try:
        with (slot_dir / "cell.json").open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def evaluate(workspace, condition, slot):
    requested_workspace = pathlib.Path(workspace).resolve()
    root = _resolve_root(requested_workspace)
    manifest = load_manifest(root)
    if condition not in manifest["conditions"] and slot != "reference":
        raise ValueError(f"unknown condition {condition}")
    slot_dir = _slot_dir(root, requested_workspace, condition, slot)
    if not slot_dir.is_dir():
        raise FileNotFoundError(f"candidate workspace does not exist: {slot_dir}")

    evaluator_dir = root / "evaluator"
    if str(evaluator_dir) not in sys.path:
        sys.path.insert(0, str(evaluator_dir))
    from instruction_audit import audit_workspace

    metadata = _cell_metadata(slot_dir)
    external_changes = _external_changes(root, requested_workspace, slot_dir, condition, slot, manifest["conditions"])
    audit = audit_workspace(slot_dir, manifest, benchmark_root=root, external_changes=external_changes)
    gate = bool(audit["passed"])
    runner = evaluator_dir / "criterion_runner.py"
    timeout = manifest["isolation"]["criterion_timeout_seconds"]
    max_output = manifest["isolation"].get("max_output_bytes", 65536)
    criteria = []
    task_results = []
    for task in manifest["tasks"]:
        solution = slot_dir / "solutions" / task["file"]
        task_criteria = []
        for criterion in task["criteria"]:
            result = _criterion(runner, criterion, solution, timeout, max_output)
            task_criteria.append(result)
            criteria.append(result)
        task_results.append({
            "id": task["id"],
            "criterion_total": len(task["criteria"]),
            "passed_criteria": sum(bool(item.get("passed")) for item in task_criteria),
            "all_passed_raw": all(bool(item.get("passed")) for item in task_criteria),
            "all_passed": bool(gate and all(bool(item.get("passed")) for item in task_criteria)),
        })

    capabilities = {}
    for kind, specs in manifest.get("capabilities", {}).items():
        values = []
        for spec in specs:
            solution = slot_dir / "solutions" / spec["file"]
            result = _criterion(runner, spec["probe"], solution, timeout, max_output)
            result["id"] = spec["id"]
            result["kind"] = kind
            values.append(result)
        capabilities[kind] = values

    usage = read_usage(slot_dir)
    raw = sum(bool(item.get("passed")) for item in criteria)
    official_total = sum(len(task["criteria"]) for task in manifest["tasks"])
    strict_tasks = sum(bool(item["all_passed"]) for item in task_results)
    official = raw if gate else 0
    extension_count = sum(bool(item.get("passed")) for item in capabilities.get("extension", []))
    resource_count = sum(bool(item.get("passed")) for item in capabilities.get("resource", []))
    completed = all(item.get("status") not in ("timeout", "runner_error", "output_limit") for item in criteria)
    tokens = usage["total_tokens"]

    def efficiency(denominator):
        return tokens / denominator if usage["available"] and denominator else None

    strict = {
        "InstructionGate": int(gate),
        "StrictTaskCount": strict_tasks,
        "OfficialCriterionCount": official,
        "ExtensionCapabilityCount": extension_count,
        "ResourceCapabilityCount": resource_count,
        "InferenceTokensPerStrictTask": efficiency(strict_tasks),
        "InferenceTokensPerPassedCriterion": efficiency(official),
    }
    lenient = {
        "ExecutionCompleted": int(completed),
        "RawCriterionCount": raw,
        "AcceptanceCoverage": raw / official_total if official_total else 0.0,
        "InstructionComplianceRate": 1.0 if gate else 0.0,
        "ExtensionCapabilityCount": extension_count,
        "InferenceTokensPerPassedCriterion": efficiency(raw),
    }
    candidate = candidate_for(manifest, slot)
    return {
        "benchmark_id": manifest["benchmark_id"],
        "schema_version": 1,
        "condition": condition,
        "slot": slot,
        "model": metadata.get("expected_model", candidate.get("expected_model", slot)),
        "group": metadata.get("group", candidate.get("group")),
        "wave": metadata.get("wave"),
        "instruction_gate": gate,
        "audit": audit,
        "criteria": criteria,
        "capabilities": capabilities,
        "task_results": task_results,
        "strict": strict,
        "lenient": lenient,
        "usage": usage,
        "official_criterion_total": official_total,
        "task_total": len(manifest["tasks"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = evaluate(args.workspace, args.condition, args.slot)
        if args.output:
            atomic_json(args.output, result)
        else:
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError) as error:
        parser.exit(2, f"evaluation failed: {error}\n")


if __name__ == "__main__":
    main()
