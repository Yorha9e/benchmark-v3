"""Static delivery-contract audit used for the Instruction Gate."""

import ast
import json
import pathlib
import sys


try:
    _STDLIB = set(sys.stdlib_module_names)
except AttributeError:
    _STDLIB = {"ast", "copy", "gc", "json", "math", "pathlib", "sys", "weakref", "collections", "functools", "itertools", "typing"}


def _issue(report, message):
    report["passed"] = False
    report["reasons"].append(message)


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return None


def _signature(node):
    arguments = node.args
    positional = [item.arg for item in arguments.posonlyargs + arguments.args]
    return positional, [item.arg for item in arguments.kwonlyargs]


def _check_signature(tree, expected, report, path):
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, names in expected.items():
        if name == "__classes__":
            continue
        node = functions.get(name)
        if node is None:
            _issue(report, f"{path.name}: missing API {name}")
            continue
        positional, keyword_only = _signature(node)
        if positional != names or keyword_only:
            _issue(report, f"{path.name}: signature mismatch for {name}")
        if name == "apply_patch" and len(node.args.defaults) != 1:
            _issue(report, f"{path.name}: apply_patch must provide a delete default")
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    for name in expected.get("__classes__", ()):
        if name not in classes:
            _issue(report, f"{path.name}: missing class {name}")


def _check_imports(tree, report, path):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name.split(".", 1)[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                _issue(report, f"{path.name}: relative imports are not allowed")
                continue
            modules = [node.module.split(".", 1)[0]] if node.module else []
        else:
            continue
        for module in modules:
            if module not in _STDLIB and module not in {"__future__"}:
                _issue(report, f"{path.name}: non-standard import {module}")


def audit_workspace(workspace, manifest, benchmark_root=None, external_changes=()):
    workspace = pathlib.Path(workspace).resolve()
    benchmark_root = pathlib.Path(benchmark_root).resolve() if benchmark_root else None
    report = {
        "passed": True,
        "reasons": [],
        "protected_files_ok": True,
        "signature_ok": True,
        "dependency_ok": True,
        "external_workspace_write": bool(tuple(external_changes)),
        "checked_solution_files": [],
    }
    if report["external_workspace_write"]:
        _issue(report, "writes outside the candidate workspace were reported")

    expected_files = {task["file"] for task in manifest["tasks"]}
    solutions = workspace / "solutions"
    if not solutions.is_dir():
        _issue(report, "missing solutions directory")
    else:
        actual_files = {path.name for path in solutions.iterdir() if path.is_file()}
        if actual_files != expected_files:
            _issue(report, "solutions directory contains unexpected or missing files")
        for child in solutions.iterdir():
            if child.is_dir() and child.name != "__pycache__":
                _issue(report, "solutions directory contains an unexpected directory")
            if child.name == "__pycache__" and any(item.suffix not in (".pyc", ".pyo") for item in child.rglob("*" ) if item.is_file()):
                _issue(report, "solutions bytecode cache contains unexpected files")

    allowed_top = {"solutions", manifest["instruction_gate"]["ack_file"], "usage.json", "events.jsonl", "cell.json", "TASKS.md", "public_smoke_tests.py", "__pycache__"}
    if workspace.is_dir():
        for path in workspace.iterdir():
            if path.name not in allowed_top:
                _issue(report, f"unexpected workspace entry {path.name}")
            if path.name == "__pycache__" and path.is_dir() and any(item.suffix not in (".pyc", ".pyo") for item in path.rglob("*") if item.is_file()):
                _issue(report, "workspace bytecode cache contains unexpected files")

    ack = _read_json(workspace / manifest["instruction_gate"]["ack_file"])
    expected_tasks = [task["id"] for task in manifest["tasks"]]
    if not isinstance(ack, dict) or ack.get("ack") != manifest["instruction_gate"]["required_text"] or ack.get(manifest["instruction_gate"]["required_tasks_field"]) != expected_tasks:
        _issue(report, "instruction acknowledgement is missing or incorrect")

    if benchmark_root:
        protected = (("TASKS.md", "TASKS.md"), ("public_smoke_tests.py", "template/public_smoke_tests.py"))
        for candidate_relative, canonical_relative in protected:
            candidate_path = workspace / candidate_relative
            canonical = benchmark_root / canonical_relative
            if candidate_path.exists() and canonical.exists():
                try:
                    if candidate_path.read_bytes() != canonical.read_bytes():
                        report["protected_files_ok"] = False
                        _issue(report, f"protected file changed: {candidate_relative}")
                except OSError:
                    report["protected_files_ok"] = False
                    _issue(report, f"protected file unreadable: {candidate_relative}")
        cell = _read_json(workspace / "cell.json")
        is_candidate_cell = (workspace / "TASKS.md").is_file()
        if is_candidate_cell and cell is None:
            report["protected_files_ok"] = False
            _issue(report, "candidate cell metadata is missing")
        if cell is not None:
            expected_cell_keys = {"benchmark_id", "condition", "slot", "expected_model", "group", "wave", "public_inputs"}
            if set(cell) != expected_cell_keys or cell.get("benchmark_id") != manifest.get("benchmark_id") or not isinstance(cell.get("condition"), str) or not isinstance(cell.get("slot"), str):
                report["protected_files_ok"] = False
                _issue(report, "cell metadata changed")
            else:
                slot = cell["slot"]
                expected_by_slot = {item["slot"]: item for item in manifest.get("candidates", [])}
                expected_conditions = {slot_id: condition for condition, slots in manifest["conditions"].items() for slot_id in slots}
                expected_waves = {slot_id: wave["id"] for wave in manifest["waves"] for slot_id in wave["slots"]}
                if (slot not in expected_by_slot or cell["condition"] != expected_conditions[slot] or cell["expected_model"] != expected_by_slot[slot]["expected_model"] or cell.get("group") != expected_by_slot[slot].get("group") or cell.get("wave") != expected_waves[slot] or cell.get("public_inputs") != ["TASKS.md", "public_smoke_tests.py"]):
                    report["protected_files_ok"] = False
                    _issue(report, "cell metadata does not match manifest")
        if (workspace / "manifest.json").exists():
            report["protected_files_ok"] = False
            _issue(report, "manifest must not be copied into a candidate workspace")

    expected_signatures = {
        "recursive_patch.py": {"apply_patch": ["base", "patch", "delete"]},
        "dependency_layers.py": {"dependency_layers": ["edges"], "__classes__": ["DependencyCycleError"]},
        "ttl_set.py": {"__classes__": ["BoundedTTLSet"]},
        "duration.py": {"parse_duration": ["text"], "normalize_duration": ["text"], "__classes__": ["DurationParseError"]},
    }
    for filename in sorted(expected_files):
        path = solutions / filename
        if not path.is_file():
            continue
        report["checked_solution_files"].append(filename)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            report["signature_ok"] = False
            _issue(report, f"{filename}: cannot parse ({type(error).__name__})")
            continue
        before = len(report["reasons"])
        _check_signature(tree, expected_signatures[filename], report, path)
        _check_imports(tree, report, path)
        if len(report["reasons"]) != before:
            report["signature_ok"] = False

    return report
