from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from common import (
    ALLOWED_SOURCE_PREFIXES,
    CLOSED_LOOP_ROOT,
    SchemaError,
    load_manifest,
    tree_hashes,
    verify_assets,
    verify_workspace_non_source,
)


PACKAGE_FILES = {
    "order_fulfillment": {
        "api": "src/order_fulfillment/__init__.py",
        "cli": "src/order_fulfillment/__main__.py",
    },
    "delivery_spool": {
        "api": "src/delivery_spool/__init__.py",
        "cli": "src/delivery_spool/__main__.py",
    },
}
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "asyncio",
        "ftplib",
        "http",
        "imaplib",
        "inspect",
        "multiprocessing",
        "nntplib",
        "poplib",
        "pty",
        "requests",
        "runpy",
        "smtplib",
        "socket",
        "subprocess",
        "telnetlib",
        "traceback",
        "urllib",
        "webbrowser",
        "xmlrpc",
    }
)
FORBIDDEN_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "builtins.eval",
        "builtins.exec",
        "builtins.compile",
        "builtins.__import__",
        "importlib.import_module",
        "os.system",
        "os.popen",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "inspect.currentframe",
        "inspect.stack",
        "inspect.trace",
        "pty.spawn",
        "runpy.run_module",
        "runpy.run_path",
        "sys._current_frames",
        "sys._getframe",
        "traceback.extract_stack",
        "traceback.walk_stack",
    }
)
FORBIDDEN_PREFIXES = (
    "os.exec",
    "os.spawn",
    "subprocess.",
    "multiprocessing.",
    "socket.",
    "http.",
    "urllib.",
    "ftplib.",
    "smtplib.",
    "imaplib.",
    "poplib.",
    "sys.__dict__.",
    "telnetlib.",
    "xmlrpc.",
)


def check(check_id: str, status: str, diagnostics: Any, *, observable: bool = True) -> dict[str, Any]:
    if status not in {"passed", "failed", "unobservable", "infrastructure_indeterminate"}:
        raise ValueError(f"invalid audit status: {status}")
    passed = {"passed": True, "failed": False}.get(status)
    return {
        "id": check_id,
        "observable": observable,
        "status": status,
        "passed": passed,
        "diagnostics": diagnostics,
    }


def _read_trees(workspace: Path) -> tuple[dict[str, ast.Module], dict[str, str]]:
    trees: dict[str, ast.Module] = {}
    errors: dict[str, str] = {}
    for relative in sorted(tree_hashes(workspace)):
        if not relative.endswith(".py") or not any(relative.startswith(prefix) for prefix in ALLOWED_SOURCE_PREFIXES):
            continue
        path = workspace / relative
        try:
            source = path.read_text(encoding="utf-8")
            trees[relative] = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as error:
            errors[relative] = f"{type(error).__name__}: {error}"
    return trees, errors


def _last_binding(body: list[ast.stmt], name: str) -> ast.AST | None:
    binding: ast.AST | None = None
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            binding = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                binding = node
        elif isinstance(node, ast.Import):
            if any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names):
                binding = node
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name != "*" and (alias.asname or alias.name) == name for alias in node.names):
                binding = node
    return binding


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    binding = _last_binding(tree.body, name)
    return binding if isinstance(binding, ast.ClassDef) else None


def _find_function(body: list[ast.stmt], name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    binding = _last_binding(body, name)
    return binding if isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _signature_shape(function: ast.FunctionDef | ast.AsyncFunctionDef | None) -> dict[str, Any] | None:
    if function is None:
        return None
    arguments = function.args
    return {
        "positional": [argument.arg for argument in arguments.posonlyargs + arguments.args],
        "positional_only_count": len(arguments.posonlyargs),
        "positional_defaults": len(arguments.defaults),
        "keyword_only": [argument.arg for argument in arguments.kwonlyargs],
        "keyword_only_defaults": [default is not None for default in arguments.kw_defaults],
        "vararg": arguments.vararg.arg if arguments.vararg else None,
        "kwarg": arguments.kwarg.arg if arguments.kwarg else None,
        "async": isinstance(function, ast.AsyncFunctionDef),
    }


def _signature_matches(
    function: ast.FunctionDef | ast.AsyncFunctionDef | None,
    positional: list[str],
    positional_defaults: int = 0,
    keyword_only: list[str] | None = None,
    keyword_only_defaults: list[bool] | None = None,
) -> bool:
    shape = _signature_shape(function)
    expected_keyword_only = keyword_only or []
    expected_keyword_defaults = keyword_only_defaults or [False] * len(expected_keyword_only)
    return bool(
        shape
        and shape["positional"] == positional
        and shape["positional_only_count"] == 0
        and shape["positional_defaults"] == positional_defaults
        and shape["keyword_only"] == expected_keyword_only
        and shape["keyword_only_defaults"] == expected_keyword_defaults
        and shape["vararg"] is None
        and shape["kwarg"] is None
        and not shape["async"]
    )


def _class_method_checks(
    tree: ast.Module,
    class_name: str,
    expected: Mapping[str, tuple[list[str], int, list[str], list[bool]]],
) -> tuple[bool, dict[str, Any]]:
    class_node = _find_class(tree, class_name)
    methods = {
        name: _find_function(class_node.body, name) if class_node is not None else None for name in expected
    }
    passed = class_node is not None and all(
        _signature_matches(methods[name], positional, defaults, keyword_only, keyword_defaults)
        for name, (positional, defaults, keyword_only, keyword_defaults) in expected.items()
    )
    return passed, {name: _signature_shape(methods[name]) for name in expected}


def public_signature_check(trees: Mapping[str, ast.Module], parse_errors: Mapping[str, str]) -> dict[str, Any]:
    order_path = PACKAGE_FILES["order_fulfillment"]["api"]
    spool_path = PACKAGE_FILES["delivery_spool"]["api"]
    details: dict[str, Any] = {"parse_errors": dict(parse_errors)}
    order_tree = trees.get(order_path)
    spool_tree = trees.get(spool_path)
    if order_tree is None or spool_tree is None:
        return check("public_exports_and_signatures", "failed", details)

    order_expected = {
        "__init__": (["self", "db_path"], 0, ["clock", "id_factory"], [False, False]),
        "set_stock": (["self", "sku", "quantity"], 0, [], []),
        "create_order": (["self", "idempotency_key", "items"], 0, [], []),
        "pay_order": (["self", "order_id", "payment_id", "amount_cents"], 0, [], []),
        "cancel_order": (["self", "order_id"], 0, [], []),
        "ship_order": (["self", "order_id"], 0, [], []),
        "get_order": (["self", "order_id"], 0, [], []),
    }
    spool_expected = {
        "__init__": (
            ["self", "root"],
            0,
            ["clock", "limits", "lock_timeout", "failpoint"],
            [False, False, False, False],
        ),
        "initialize": (["self"], 0, [], []),
        "enqueue": (["self", "message_id", "payload", "available_at"], 1, [], []),
        "claim": (["self", "worker_id"], 0, [], []),
        "ack": (["self", "message_id", "lease_token"], 0, [], []),
        "fail": (["self", "message_id", "lease_token", "error"], 0, [], []),
        "recover": (["self"], 0, [], []),
        "get": (["self", "message_id"], 0, [], []),
        "list_messages": (["self", "status"], 1, [], []),
    }
    order_passed, order_shapes = _class_method_checks(order_tree, "OrderService", order_expected)
    spool_passed, spool_shapes = _class_method_checks(spool_tree, "Spool", spool_expected)
    order_error = _find_class(order_tree, "OrderError")
    spool_error = _find_class(spool_tree, "SpoolError")
    limits = _find_class(spool_tree, "Limits")
    details.update({"OrderService": order_shapes, "Spool": spool_shapes})
    passed = all(
        (
            order_passed,
            spool_passed,
            order_error is not None,
            spool_error is not None,
            limits is not None,
        )
    )
    return check("public_exports_and_signatures", "passed" if passed else "failed", details)


def _aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        resolved = None
        if node.value is not None:
            resolved = (
                _dotted_name(node.value, aliases)
                or _dynamic_member_name(node.value, aliases)
                or _constant_string(node.value, aliases)
            )
        if resolved:
            for target in targets:
                if isinstance(target, ast.Name):
                    existing = aliases.get(target.id)
                    if existing is None or not _security_sensitive_alias(existing):
                        aliases[target.id] = resolved
    return aliases


def _dotted_name(node: ast.AST, aliases: Mapping[str, str]) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    base = aliases.get(current.id, current.id)
    return ".".join((base, *reversed(parts)))


def _security_sensitive_alias(value: str) -> bool:
    return (
        value in {"sys", "inspect", "traceback", "runpy", "_getframe"}
        or value in FORBIDDEN_CALLS
        or value.endswith(".__dict__")
        or any(value.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
    )


def _constant_string(node: ast.AST, aliases: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left, aliases)
        right = _constant_string(node.right, aliases)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_constant_string(value, aliases) for value in node.values]
        if all(part is not None for part in parts):
            return "".join(part for part in parts if part is not None)
    return None


def _dynamic_member_name(node: ast.AST, aliases: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Call):
        getter = _dotted_name(node.func, aliases)
        if getter == "getattr" and len(node.args) >= 2:
            owner = _dotted_name(node.args[0], aliases) or _dynamic_member_name(node.args[0], aliases)
            member = _constant_string(node.args[1], aliases)
            if owner and member is not None:
                return f"{owner}.{member}"
        if getter == "vars" and len(node.args) == 1:
            owner = _dotted_name(node.args[0], aliases)
            if owner:
                return f"{owner}.__dict__"
        if getter and getter.endswith(".__dict__.get") and node.args:
            member = _constant_string(node.args[0], aliases)
            if member is not None:
                return f"{getter[:-13]}.{member}"
    if isinstance(node, ast.Subscript):
        owner = _dotted_name(node.value, aliases) or _dynamic_member_name(node.value, aliases)
        member = _constant_string(node.slice, aliases)
        if owner and owner.endswith(".__dict__") and member is not None:
            return f"{owner[:-9]}.{member}"
    return None


def import_and_forbidden_behavior_check(trees: Mapping[str, ast.Module]) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    import_violations: list[dict[str, Any]] = []
    behavior_violations: list[dict[str, Any]] = []
    imports: dict[str, list[dict[str, Any]]] = {}
    for relative, tree in trees.items():
        aliases = _aliases(tree)
        module_imports: list[dict[str, Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    item = {"root": root, "source": alias.name, "line": node.lineno}
                    module_imports.append(item)
                    if root not in allowed:
                        import_violations.append({"path": relative, **item})
                    if root in FORBIDDEN_IMPORT_ROOTS:
                        behavior_violations.append({"path": relative, "kind": "forbidden_import", **item})
            elif isinstance(node, ast.ImportFrom):
                source = "." * node.level + (node.module or "")
                root = "<relative>" if node.level else source.split(".")[0]
                item = {"root": root, "source": source, "line": node.lineno}
                module_imports.append(item)
                if not node.level and root not in allowed:
                    import_violations.append({"path": relative, **item})
                if not node.level and root in FORBIDDEN_IMPORT_ROOTS:
                    behavior_violations.append({"path": relative, "kind": "forbidden_import", **item})
            elif isinstance(node, ast.Call):
                name = _dotted_name(node.func, aliases) or _dynamic_member_name(node.func, aliases)
                introspection_owner = None
                if name in {"getattr", "vars"} and node.args:
                    introspection_owner = _dotted_name(node.args[0], aliases) or _dynamic_member_name(
                        node.args[0], aliases
                    )
                forbidden_introspection = introspection_owner in {"sys", "inspect", "traceback", "runpy"}
                if (
                    name in FORBIDDEN_CALLS
                    or forbidden_introspection
                    or (name and any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES))
                ):
                    call_name = f"{name}({introspection_owner})" if forbidden_introspection else name
                    behavior_violations.append(
                        {"path": relative, "kind": "forbidden_call", "call": call_name, "line": node.lineno}
                    )
        imports[relative] = module_imports
    import_result = check(
        "standard_library_only_imports",
        "passed" if not import_violations else "failed",
        {"imports": imports, "violations": import_violations},
    )
    behavior_result = check(
        "no_forbidden_network_process_or_dynamic_code",
        "passed" if not behavior_violations else "failed",
        {"violations": behavior_violations},
    )
    return import_result, behavior_result


def audit_workspace(workspace: Path, slot: str, root: Path = CLOSED_LOOP_ROOT) -> dict[str, Any]:
    manifest = load_manifest(root)
    if slot not in manifest["candidate_slots"]:
        raise SchemaError(f"unknown slot: {slot!r}")
    workspace = workspace.resolve()
    checks: list[dict[str, Any]] = []

    try:
        asset_violations = verify_assets(root)
        checks.append(
            check(
                "frozen_assets",
                "passed" if not asset_violations else "infrastructure_indeterminate",
                asset_violations,
            )
        )
    except BaseException as error:
        checks.append(
            check("frozen_assets", "infrastructure_indeterminate", f"{type(error).__name__}: {error}")
        )

    if not workspace.is_dir():
        checks.append(check("workspace_exists", "failed", f"workspace missing: {workspace}"))
        checks.append(check("workspace_non_source", "failed", "workspace unavailable"))
        trees: dict[str, ast.Module] = {}
        parse_errors = {path: "workspace unavailable" for files in PACKAGE_FILES.values() for path in files.values()}
    else:
        checks.append(check("workspace_exists", "passed", workspace.as_posix()))
        try:
            workspace_violations = verify_workspace_non_source(workspace, root)
            checks.append(
                check(
                    "workspace_non_source",
                    "passed" if not workspace_violations else "failed",
                    workspace_violations,
                )
            )
        except BaseException as error:
            checks.append(
                check(
                    "workspace_non_source",
                    "infrastructure_indeterminate",
                    f"{type(error).__name__}: {error}",
                )
            )
        trees, parse_errors = _read_trees(workspace)

    required_files = {relative for files in PACKAGE_FILES.values() for relative in files.values()}
    missing_or_unparsed = sorted(required_files - trees.keys())
    checks.append(
        check(
            "allowed_packages_parse",
            "passed" if not parse_errors and not missing_or_unparsed else "failed",
            {"missing_or_unparsed": missing_or_unparsed, "errors": parse_errors},
        )
    )
    checks.append(public_signature_check(trees, parse_errors))
    import_result, behavior_result = import_and_forbidden_behavior_check(trees)
    checks.extend((import_result, behavior_result))
    checks.extend(
        (
            check(
                "process_no_delegation_attempt",
                "unobservable",
                "Not observable from final workspace files.",
                observable=False,
            ),
            check(
                "process_no_hidden_evaluator_read_attempt",
                "unobservable",
                "Not observable from final workspace files.",
                observable=False,
            ),
            check(
                "process_no_network_attempt",
                "unobservable",
                "AST-visible behavior is audited, but execution history is not observable from final files.",
                observable=False,
            ),
            check(
                "writes_only_declared_storage",
                "unobservable",
                "Final workspace files cannot prove that runtime writes stayed within declared storage.",
                observable=False,
            ),
        )
    )
    observable_statuses = [item["status"] for item in checks if item["observable"]]
    if "infrastructure_indeterminate" in observable_statuses:
        instruction_gate: bool | None = None
    else:
        instruction_gate = all(status == "passed" for status in observable_statuses)
    return {
        "schema_version": manifest["schema_version"],
        "benchmark_version": manifest["benchmark_version"],
        "slot": slot,
        "workspace": workspace.as_posix(),
        "checks": checks,
        "instruction_gate": instruction_gate,
        "decision_infrastructure_indeterminate": instruction_gate is None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Mechanically audit closed-loop v2 candidate instructions.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--slot", required=True)
    args = parser.parse_args()
    print(json.dumps(audit_workspace(args.workspace, args.slot), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
