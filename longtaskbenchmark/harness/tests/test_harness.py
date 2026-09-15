from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[3]
HARNESS_ROOT = Path(__file__).resolve().parents[1]
CLOSED_LOOP_ROOT = HARNESS_ROOT.parent
sys.path.insert(0, str(HARNESS_ROOT))

from build_report import (  # noqa: E402
    audit_legacy_hashes,
    build_rows,
    rank_rows,
    select_finalists,
)
from common import (  # noqa: E402
    FINAL_REPORT_SCHEMA,
    ISOLATED_ENV_REMOVED,
    RANKING_AXES,
    SchemaError,
    atomic_write_json,
    build_briefing_bytes,
    changed_paths,
    isolated_python_env,
    load_json,
    load_manifest,
    ordered_slots,
    sha256_file,
    tree_snapshot,
    validate_agent_map,
    validate_criteria,
    validate_manifest,
)
from compare_rounds import pair_relation, pairwise_concordance, top_set_movement  # noqa: E402
from evaluate import _terminate_process_tree, evaluate, run_criterion  # noqa: E402
from extract_usage import aggregate_wire, extract_usage  # noqa: E402
from freeze_assets import CONTROLLED_DIRECTORIES, run_reference_validation  # noqa: E402
from instruction_audit import (  # noqa: E402
    _read_trees,
    audit_workspace,
    import_and_forbidden_behavior_check,
    public_signature_check,
)
from prepare_runs import dispatch_order, prepare  # noqa: E402


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def valid_reference_validation() -> dict:
    criteria_manifest = load_json(CLOSED_LOOP_ROOT / "evaluator" / "criteria.json")
    mutant_manifest = load_json(CLOSED_LOOP_ROOT / "validation" / "mutants" / "manifest.json")
    criterion_evidence = [
        {
            "criterion": criterion["id"],
            "target": criterion["unittest"],
            "status": "passed",
            "passed": True,
            "executed": True,
            "tests_run": 1,
            "returncode": 0,
        }
        for project in criteria_manifest["projects"]
        for milestone in project["milestones"]
        for criterion in milestone["criteria"]
    ]
    return {
        "reference_runs": [
            {"run": index, "passed": 20, "total": 20, "criteria": copy.deepcopy(criterion_evidence)}
            for index in (1, 2)
        ],
        "normalized_consistent": True,
        "criteria": {"passed": 20, "total": 20},
        "reference_score": "20/20",
        "mutants": [
            {
                "id": mutant["id"],
                "criterion": mutant["target_criterion"],
                "detected": True,
                "status": "detected",
                "diagnostic": "Ran 1 test; FAILED",
            }
            for mutant in mutant_manifest["mutants"]
        ],
        "overall_passed": True,
    }


def executor_record(manifest: dict, slot: str, index: int) -> dict:
    record = {
        "slot": slot,
        "agent_id": f"agent-{index}",
        "model": manifest["candidate_slots"][slot]["model"],
        "status": "completed",
        "wave": next(name for name, members in manifest["waves"].items() if slot in members),
    }
    effort = manifest["candidate_slots"][slot].get("thinking_effort")
    if effort is not None:
        record["thinking_effort"] = effort
    return record


def complete_instruction_checks() -> list[dict]:
    definitions = (
        ("frozen_assets", True),
        ("workspace_exists", True),
        ("workspace_non_source", True),
        ("allowed_packages_parse", True),
        ("public_exports_and_signatures", True),
        ("standard_library_only_imports", True),
        ("no_forbidden_network_process_or_dynamic_code", True),
        ("process_no_delegation_attempt", False),
        ("process_no_hidden_evaluator_read_attempt", False),
        ("process_no_network_attempt", False),
        ("writes_only_declared_storage", False),
    )
    return [
        {
            "id": check_id,
            "observable": observable,
            "status": "passed" if observable else "unobservable",
            "passed": True if observable else None,
            "diagnostics": "ok",
        }
        for check_id, observable in definitions
    ]


def copy_contract(root: Path) -> tuple[dict, dict]:
    manifest = load_json(CLOSED_LOOP_ROOT / "manifest.json")
    criteria = load_json(CLOSED_LOOP_ROOT / "evaluator" / "criteria.json")
    write_json(root / "manifest.json", manifest)
    write_json(root / "evaluator" / "criteria.json", criteria)
    return manifest, criteria


def complete_evaluations_and_usages(manifest: dict) -> tuple[list[dict], list[dict]]:
    criteria_manifest = load_json(CLOSED_LOOP_ROOT / "evaluator" / "criteria.json")
    empty_tree = {"file_count": 0, "tree_digest": hashlib.sha256(b"{}").hexdigest(), "files": {}}
    complete_projects = []
    for expected_project in criteria_manifest["projects"]:
        milestones = []
        for expected_milestone in expected_project["milestones"]:
            criterion_results = [
                {
                    "id": criterion["id"],
                    "capability_id": criterion["capability_id"],
                    "unittest": criterion["unittest"],
                    "status": "passed",
                    "passed": True,
                    "returncode": 0,
                    "duration_ms": 1.0,
                    "diagnostics": "ok",
                    "command": ["python", "-m", "unittest", criterion["unittest"], "-v"],
                }
                for criterion in expected_milestone["criteria"]
            ]
            milestones.append(
                {
                    "id": expected_milestone["id"],
                    "capability_id": expected_milestone["capability_id"],
                    "strict_success": True,
                    "criteria": criterion_results,
                }
            )
        complete_projects.append(
            {"id": expected_project["id"], "closed_loop_success": True, "milestones": milestones}
        )

    evaluations = []
    usages = []
    for index, slot in enumerate(ordered_slots(manifest), start=1):
        evaluations.append(
            {
                "schema_version": manifest["schema_version"],
                "benchmark_version": manifest["benchmark_version"],
                "evaluated_at": "2026-01-01T00:00:00+00:00",
                "slot": slot,
                "model": manifest["candidate_slots"][slot]["model"],
                "workspace": f"/workspace/{slot}",
                "instruction": {
                    "schema_version": manifest["schema_version"],
                    "benchmark_version": manifest["benchmark_version"],
                    "slot": slot,
                    "workspace": f"/workspace/{slot}",
                    "checks": complete_instruction_checks(),
                    "instruction_gate": True,
                    "decision_infrastructure_indeterminate": False,
                },
                "InstructionGate": True,
                "ClosedLoopProjectCount": len(manifest["projects"]),
                "MilestoneStrictCount": manifest["milestone_count"],
                "AcceptanceCoverage": 1.0,
                "criterion_pass_count": manifest["criterion_count"],
                "criterion_count": manifest["criterion_count"],
                "decision_infrastructure_indeterminate": False,
                "infrastructure_indeterminate_evidence": [],
                "projects": copy.deepcopy(complete_projects),
                "workspace_before": copy.deepcopy(empty_tree),
                "workspace_after": copy.deepcopy(empty_tree),
                "workspace_changed_paths": [],
                "workspace_unchanged_during_evaluation": True,
            }
        )
        usages.append(
            {
                "schema_version": manifest["schema_version"],
                "role": "executor",
                "slot": slot,
                "model": manifest["candidate_slots"][slot]["model"],
                "token_measurement_status": "valid",
                "token_measurement_valid": True,
                "inference_tokens": index * 100,
                "input_context_tokens": index * 80,
                "output_tokens": index * 20,
                "fresh_tokens": index * 50,
            }
        )
    return evaluations, usages


class SchemaTests(unittest.TestCase):
    def test_manifest_enforces_exact_axes_and_model_schema(self):
        manifest = load_json(CLOSED_LOOP_ROOT / "manifest.json")
        self.assertEqual(validate_manifest(copy.deepcopy(manifest))["schema_version"], 2)
        changed = copy.deepcopy(manifest)
        changed["ranking_axes"][0], changed["ranking_axes"][1] = (
            changed["ranking_axes"][1],
            changed["ranking_axes"][0],
        )
        with self.assertRaises(SchemaError):
            validate_manifest(changed)
        changed = copy.deepcopy(manifest)
        first_slot = next(iter(changed["candidate_slots"]))
        changed["candidate_slots"][first_slot]["unexpected"] = True
        with self.assertRaises(SchemaError):
            validate_manifest(changed)

    def test_criteria_counts_are_derived_and_capabilities_are_strict(self):
        manifest = load_manifest()
        criteria = load_json(CLOSED_LOOP_ROOT / "evaluator" / "criteria.json")
        validated = validate_criteria(copy.deepcopy(criteria), manifest)
        derived = sum(
            len(milestone["criteria"])
            for project in validated["projects"]
            for milestone in project["milestones"]
        )
        self.assertEqual(validated["criterion_count"], derived)
        changed = copy.deepcopy(criteria)
        changed["projects"][0]["milestones"][0]["criteria"][0]["capability_id"] = ""
        with self.assertRaises(SchemaError):
            validate_criteria(changed, manifest)

    def test_agent_map_requires_exact_model_wave_and_thinking_effort(self):
        manifest = load_manifest()
        executors = [
            executor_record(manifest, slot, index)
            for index, slot in enumerate(ordered_slots(manifest), start=1)
        ]
        mapping = {"schema_version": manifest["schema_version"], "executors": executors}
        self.assertEqual(len(validate_agent_map(mapping, manifest)["executors"]), len(executors))
        for field, value in (("model", "wrong/model"), ("wave", "wrong-wave")):
            changed = copy.deepcopy(mapping)
            changed["executors"][0][field] = value
            with self.assertRaises(SchemaError):
                validate_agent_map(changed, manifest)

        changed = copy.deepcopy(mapping)
        del changed["executors"][0]["thinking_effort"]
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)
        no_effort_index = ordered_slots(manifest).index("subtest_2")
        changed = copy.deepcopy(mapping)
        changed["executors"][no_effort_index]["thinking_effort"] = "high"
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)
        changed = copy.deepcopy(mapping)
        del changed["executors"][0]["wave"]
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)

    def test_agent_map_retry_evidence_prevents_unaccounted_previous_tokens(self):
        manifest = load_manifest()
        mapping = {
            "schema_version": manifest["schema_version"],
            "executors": [
                executor_record(manifest, slot, index)
                for index, slot in enumerate(ordered_slots(manifest), start=1)
            ],
        }
        retried = mapping["executors"][0]
        retried.update(
            {
                "retry_count": 1,
                "replacement_for_agent_id": "failed-agent",
                "previous_model_response_started": False,
            }
        )
        validate_agent_map(mapping, manifest)

        changed = copy.deepcopy(mapping)
        changed["executors"][0]["previous_model_response_started"] = True
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)
        changed = copy.deepcopy(mapping)
        del changed["executors"][0]["replacement_for_agent_id"]
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)
        changed = copy.deepcopy(mapping)
        changed["executors"][0]["retry_count"] = 0
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)
        changed = copy.deepcopy(mapping)
        changed["executors"][0]["retry_count"] = 2
        with self.assertRaises(SchemaError):
            validate_agent_map(changed, manifest)


class CommonTests(unittest.TestCase):
    def test_isolated_environment_removes_host_python_state_and_sets_controls(self):
        injected = {key: "host-value" for key in ISOLATED_ENV_REMOVED}
        with mock.patch.dict(os.environ, injected, clear=False):
            env = isolated_python_env((Path("alpha"), Path("beta")))
        for key in ISOLATED_ENV_REMOVED:
            if key == "PYTHONPATH":
                self.assertNotIn("host-value", env[key])
            else:
                self.assertNotIn(key, env)
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTHONHASHSEED"], "0")
        self.assertEqual(len(env["PYTHONPATH"].split(os.pathsep)), 2)

    def test_atomic_json_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "result.json"
            atomic_write_json(path, {"version": 1, "value": [1, 2]})
            atomic_write_json(path, {"version": 2, "value": []})
            self.assertEqual(load_json(path), {"version": 2, "value": []})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_tree_snapshot_is_complete_and_runtime_artifacts_are_symmetric(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "a" / "one.txt").write_text("one", encoding="utf-8")
            (root / "two.bin").write_bytes(b"two")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "ignored.pyc").write_bytes(b"runtime")
            before = tree_snapshot(root)
            (root / "a" / "one.txt").write_text("changed", encoding="utf-8")
            after = tree_snapshot(root)
            self.assertEqual(set(before["files"]), {"a/one.txt", "two.bin"})
            self.assertEqual(changed_paths(before["files"], after["files"]), ["a/one.txt"])
            self.assertNotEqual(before["tree_digest"], after["tree_digest"])

    def test_briefing_concatenates_raw_inputs_in_required_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = copy_contract(root)
            (root / "template").mkdir(exist_ok=True)
            constraints = b"CONSTRAINTS\r\nraw\r\n"
            tasks = b"TASKS\nraw\x7f\n\n"
            plan = b"PLAN\r\nraw\r"
            (root / "template" / "BRIEFING.preamble.md").write_bytes(constraints)
            (root / "template" / "TASKS.md").write_bytes(tasks)
            plan_path = root / manifest["paths"]["frozen_plan"]
            plan_path.parent.mkdir(parents=True)
            plan_path.write_bytes(plan)
            briefing = build_briefing_bytes(root)
            offsets = [briefing.index(item) for item in (constraints, tasks, plan, FINAL_REPORT_SCHEMA)]
            self.assertEqual(offsets, sorted(offsets))
            self.assertEqual(briefing.count(constraints), 1)
            self.assertEqual(briefing.count(tasks), 1)
            self.assertEqual(briefing.count(plan), 1)
            self.assertTrue(briefing.endswith(FINAL_REPORT_SCHEMA))

    def test_reference_validation_is_strict_and_validation_is_frozen(self):
        self.assertIn("validation", CONTROLLED_DIRECTORIES)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copy_contract(root)
            write_json(
                root / "validation" / "mutants" / "manifest.json",
                load_json(CLOSED_LOOP_ROOT / "validation" / "mutants" / "manifest.json"),
            )
            script = root / "validation" / "run_validation.py"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("raise AssertionError('mocked')\n", encoding="utf-8")
            summary = valid_reference_validation()
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps(summary, separators=(",", ":")) + "\n",
                stderr="",
            )
            with mock.patch("freeze_assets.subprocess.run", return_value=completed) as run:
                result = run_reference_validation(root)
            self.assertEqual(result, summary)
            self.assertEqual(load_json(root / "validation" / "reference-validation.json"), summary)
            self.assertEqual(run.call_args.kwargs["cwd"], root / "validation")

            bad = copy.deepcopy(summary)
            bad["mutants"][0]["detected"] = False
            completed.stdout = json.dumps(bad) + "\n"
            with mock.patch("freeze_assets.subprocess.run", return_value=completed):
                with self.assertRaises(SchemaError):
                    run_reference_validation(root)

            bad = copy.deepcopy(summary)
            bad["reference_runs"][0]["criteria"][0] = {"passed": True}
            completed.stdout = json.dumps(bad) + "\n"
            with mock.patch("freeze_assets.subprocess.run", return_value=completed):
                with self.assertRaises(SchemaError):
                    run_reference_validation(root)

    def test_dispatch_order_preserves_manifest_wave_order(self):
        manifest = load_manifest()
        evidence = dispatch_order(manifest)
        self.assertEqual([item["wave"] for item in evidence], list(manifest["waves"]))
        self.assertEqual([item["sequence"] for item in evidence], list(range(1, len(evidence) + 1)))
        self.assertEqual([item["slots"] for item in evidence], list(manifest["waves"].values()))

    def test_prepare_missing_plan_creates_no_workspace_or_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copy_contract(root)
            with self.assertRaises(FileNotFoundError):
                prepare(root)
            self.assertFalse((root / "runs").exists())
            self.assertFalse((root / "results").exists())

    def test_prepare_publication_failure_rolls_back_only_new_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "closed_loop_v2"
            manifest, _ = copy_contract(root)
            template = root / "template"
            template.mkdir()
            (template / "BRIEFING.preamble.md").write_text("constraints\n", encoding="utf-8")
            (template / "TASKS.md").write_text("tasks\n", encoding="utf-8")
            plan = root / manifest["paths"]["frozen_plan"]
            plan.parent.mkdir(parents=True)
            plan.write_text("plan\n", encoding="utf-8")
            briefing = build_briefing_bytes(root)
            (template / "BRIEFING.md").write_bytes(briefing)
            (root / manifest["paths"]["briefing"]).write_bytes(briefing)
            write_json(
                root / "validation" / "mutants" / "manifest.json",
                load_json(CLOSED_LOOP_ROOT / "validation" / "mutants" / "manifest.json"),
            )
            write_json(root / "validation" / "reference-validation.json", valid_reference_validation())

            real_replace = os.replace
            workspace_publications = 0

            def fail_second_workspace(source, destination):
                nonlocal workspace_publications
                source_path = Path(source)
                if "workspaces" in source_path.parts and source_path.name == "workspace":
                    workspace_publications += 1
                    if workspace_publications == 2:
                        raise OSError("injected publication failure")
                return real_replace(source, destination)

            with mock.patch("prepare_runs.verify_assets", return_value=[]), mock.patch(
                "prepare_runs.os.replace", side_effect=fail_second_workspace
            ):
                with self.assertRaises(OSError):
                    prepare(root)

            for name in ("expected-cells.jsonl", "prepared-workspaces.json", "assignments.json"):
                self.assertFalse((root / "results" / name).exists())
            self.assertFalse((root / "runs").exists())
            self.assertEqual((template / "BRIEFING.md").read_bytes(), briefing)
            self.assertEqual((root / manifest["paths"]["briefing"]).read_bytes(), briefing)
            self.assertEqual(list(root.glob(".prepare-runs-*")), [])


class IsolationEvaluationTests(unittest.TestCase):
    def test_run_criterion_uses_minimal_temporary_evaluator_bundle(self):
        process = mock.Mock(pid=1234, returncode=0)
        process.communicate.return_value = ("ok", "")
        observed: dict[str, object] = {}

        def launch(command, **kwargs):
            bundle_root = Path(kwargs["cwd"])
            observed.update({"command": command, "cwd": bundle_root, "env": kwargs["env"]})
            self.assertTrue((bundle_root / "closed_loop_v2" / "__init__.py").is_file())
            self.assertTrue((bundle_root / "closed_loop_v2" / "evaluator").is_dir())
            self.assertFalse((bundle_root / "closed_loop_v2" / "evaluator" / "criteria.json").exists())
            self.assertFalse((bundle_root / "closed_loop_v2" / "harness").exists())
            self.assertFalse((bundle_root / "closed_loop_v2" / "validation").exists())
            self.assertFalse((bundle_root / "closed_loop_v2" / "manifest.json").exists())
            return process

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with mock.patch("evaluate.subprocess.Popen", side_effect=launch) as popen:
                result = run_criterion(workspace, "closed_loop_v2.evaluator.test_demo.Case.test_it")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(popen.call_count, 1)
        env = observed["env"]
        self.assertIsInstance(env, dict)
        for key in ISOLATED_ENV_REMOVED[1:]:
            self.assertNotIn(key, env)
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTHONHASHSEED"], "0")
        explicit_paths = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(explicit_paths[0], str(workspace.resolve()))
        self.assertEqual(explicit_paths[1], str(observed["cwd"]))
        self.assertNotIn(str(PROJECT_ROOT.resolve()), explicit_paths)
        self.assertNotEqual(observed["cwd"], PROJECT_ROOT.resolve())
        self.assertFalse(Path(observed["cwd"]).exists())

    def test_timeout_cleans_process_tree_and_is_infrastructure_indeterminate(self):
        process = mock.Mock(pid=1234, returncode=None)
        process.communicate.side_effect = __import__("subprocess").TimeoutExpired(["python"], 1)
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "evaluate.subprocess.Popen", return_value=process
        ) as popen, mock.patch(
            "evaluate._terminate_process_tree", return_value="process tree terminated"
        ) as terminate:
            result = run_criterion(Path(temporary), "closed_loop_v2.evaluator.test_demo.Case.test_it")
        self.assertEqual(result["status"], "infrastructure_indeterminate")
        self.assertIsNone(result["passed"])
        self.assertIn("process tree terminated", result["diagnostics"])
        terminate.assert_called_once_with(process)
        if os.name == "nt":
            self.assertEqual(
                popen.call_args.kwargs["creationflags"],
                __import__("subprocess").CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_windows_process_tree_cleanup_uses_taskkill(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.communicate.return_value = ("", "")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("evaluate.os.name", "nt"), mock.patch(
            "evaluate.subprocess.run", return_value=completed
        ) as run:
            diagnostics = _terminate_process_tree(process)
        self.assertEqual(diagnostics, "process tree terminated")
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4321", "/T", "/F"])

    def test_evaluate_derives_all_denominators_from_criteria(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "closed_loop_v2"
            workspace = Path(temporary) / "workspace"
            workspace.mkdir(parents=True)
            manifest, criteria = copy_contract(root)
            slot = ordered_slots(manifest)[0]
            instruction = {
                "schema_version": manifest["schema_version"],
                "benchmark_version": manifest["benchmark_version"],
                "slot": slot,
                "workspace": workspace.resolve().as_posix(),
                "checks": [],
                "instruction_gate": True,
                "decision_infrastructure_indeterminate": False,
            }
            passed = {
                "status": "passed",
                "passed": True,
                "returncode": 0,
                "duration_ms": 0.1,
                "diagnostics": "ok",
                "command": ["python"],
            }
            with mock.patch("evaluate.audit_workspace", return_value=instruction), mock.patch(
                "evaluate.run_criterion", return_value=passed
            ):
                result = evaluate(workspace, slot, root=root)
            derived_milestones = sum(len(project["milestones"]) for project in criteria["projects"])
            derived_criteria = sum(
                len(milestone["criteria"])
                for project in criteria["projects"]
                for milestone in project["milestones"]
            )
            self.assertEqual(result["MilestoneStrictCount"], derived_milestones)
            self.assertEqual(result["criterion_count"], derived_criteria)
            self.assertEqual(result["AcceptanceCoverage"], 1.0)
            self.assertEqual(result["workspace_before"]["files"], result["workspace_after"]["files"])


class InstructionAuditTests(unittest.TestCase):
    def test_template_public_signatures_match_contract(self):
        trees, errors = _read_trees(CLOSED_LOOP_ROOT / "template")
        result = public_signature_check(trees, errors)
        self.assertEqual(result["status"], "passed", result["diagnostics"])

    def test_relative_internal_import_is_allowed_but_forbidden_introspection_fails(self):
        trees = {
            "src/order_fulfillment/helper.py": ast.parse("from .local import value\n"),
            "src/delivery_spool/bad.py": ast.parse(
                "import socket, inspect, traceback, runpy, sys\n"
                "eval('1')\nsys._getframe()\n"
                "frame_getter = getattr(sys, '_getframe')\nframe_getter()\n"
                "sys.__dict__['_getframe']()\n"
                "getattr(sys, '_' + 'getframe')()\n"
                "sys.__dict__['_get' + 'frame']()\n"
                "sys.__dict__.get('_getframe')()\n"
                "vars(sys)['_getframe']()\n"
                "def hidden():\n    from os import system as harmless\n    harmless('x')\n"
            ),
        }
        import_result, behavior_result = import_and_forbidden_behavior_check(trees)
        self.assertEqual(import_result["status"], "passed")
        self.assertEqual(behavior_result["status"], "failed")
        violations = behavior_result["diagnostics"]["violations"]
        kinds = {item["kind"] for item in violations}
        self.assertEqual(kinds, {"forbidden_import", "forbidden_call"})
        calls = {item.get("call") for item in violations}
        self.assertIn("os.system", calls)
        self.assertIn("sys._getframe", calls)
        forbidden_roots = {item.get("root") for item in violations if item["kind"] == "forbidden_import"}
        self.assertTrue({"socket", "inspect", "traceback", "runpy"}.issubset(forbidden_roots))

    def test_frame_introspection_variable_keys_are_detected_per_file(self):
        trees = {
            "src/order_fulfillment/direct.py": ast.parse(
                "import sys\nmember = '_getframe'\nsys.__dict__[member]()\nmember = 'safe'\n"
            ),
            "src/order_fulfillment/alias.py": ast.parse(
                "import sys\nnamespace = sys.__dict__\nmember = '_getframe'\n"
                "namespace[member]()\nnamespace = dict\nmember = 'safe'\n"
            ),
            "src/delivery_spool/getter.py": ast.parse(
                "import sys\nmember = '_get' + 'frame'\ngetattr(sys, member)()\n"
            ),
        }
        _, behavior_result = import_and_forbidden_behavior_check(trees)
        violating_paths = {
            item["path"]
            for item in behavior_result["diagnostics"]["violations"]
            if item["kind"] == "forbidden_call"
        }
        self.assertEqual(violating_paths, set(trees))

    def test_declared_storage_write_check_is_explicitly_unobservable(self):
        slot = ordered_slots(load_manifest())[0]
        with mock.patch("instruction_audit.verify_assets", return_value=[]), mock.patch(
            "instruction_audit.verify_workspace_non_source", return_value=[]
        ):
            result = audit_workspace(CLOSED_LOOP_ROOT / "template", slot)
        check_by_id = {item["id"]: item for item in result["checks"]}
        storage = check_by_id["writes_only_declared_storage"]
        self.assertEqual(storage["status"], "unobservable")
        self.assertIsNone(storage["passed"])
        self.assertFalse(storage["observable"])


class UsageTests(unittest.TestCase):
    def test_only_strict_usage_records_are_aggregated(self):
        with tempfile.TemporaryDirectory() as temporary:
            wire = Path(temporary) / "wire.jsonl"
            events = [
                {"type": "other", "usage": {"output": 999}},
                {
                    "type": "usage.record",
                    "model": "demo/model",
                    "usage": {"inputOther": 2, "inputCacheRead": 3, "inputCacheCreation": 5, "output": 7},
                },
                {
                    "type": "usage.record",
                    "model": "demo/model",
                    "usage": {"inputOther": 11, "inputCacheRead": 13, "inputCacheCreation": 17, "output": 19},
                },
            ]
            wire.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            usage = aggregate_wire(wire, "demo/model")
            self.assertTrue(usage["token_measurement_valid"])
            self.assertEqual(usage["input_context_tokens"], 51)
            self.assertEqual(usage["output_tokens"], 26)
            self.assertEqual(usage["inference_tokens"], 77)
            self.assertEqual(usage["fresh_tokens"], 61)

    def test_invalid_line_or_model_makes_measurement_indeterminate_not_infinite(self):
        with tempfile.TemporaryDirectory() as temporary:
            wire = Path(temporary) / "wire.jsonl"
            wire.write_text(
                "not-json\n"
                + json.dumps(
                    {
                        "type": "usage.record",
                        "model": "wrong/model",
                        "usage": {"inputOther": 1, "inputCacheRead": 1, "inputCacheCreation": 1, "output": 1},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            usage = aggregate_wire(wire, "expected/model")
            self.assertEqual(usage["token_measurement_status"], "indeterminate")
            self.assertIsNone(usage["inference_tokens"])
            self.assertGreaterEqual(len(usage["measurement_errors"]), 2)

    def test_extract_usage_excludes_main_agent_from_candidate_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "closed_loop_v2"
            manifest, _ = copy_contract(root)
            agents = base / "agents"
            executors = []
            for index, slot in enumerate(ordered_slots(manifest), start=1):
                executor = executor_record(manifest, slot, index)
                executors.append(executor)
                agent_id = executor["agent_id"]
                model = executor["model"]
                wire = agents / agent_id / "wire.jsonl"
                wire.parent.mkdir(parents=True)
                wire.write_text(
                    json.dumps(
                        {
                            "type": "usage.record",
                            "model": model,
                            "usage": {"inputOther": 1, "inputCacheRead": 2, "inputCacheCreation": 3, "output": 4},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            main_agent = {"agent_id": "main", "model": "main/model", "status": "completed"}
            main_wire = agents / "main" / "wire.jsonl"
            main_wire.parent.mkdir(parents=True)
            main_wire.write_text(
                json.dumps(
                    {
                        "type": "usage.record",
                        "model": "main/model",
                        "usage": {"inputOther": 100, "inputCacheRead": 100, "inputCacheCreation": 100, "output": 100},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            agent_map = base / "agent-map.json"
            write_json(
                agent_map,
                {"schema_version": manifest["schema_version"], "main_agent": main_agent, "executors": executors},
            )
            records, main_record = extract_usage(agent_map, agents, root=root)
            self.assertEqual(len(records), len(executors))
            self.assertTrue(all(record["role"] == "executor" for record in records))
            self.assertNotIn("main", {record["agent_id"] for record in records})
            self.assertTrue(main_record["excluded_from_candidate_ranking"])


class RankingTests(unittest.TestCase):
    @staticmethod
    def row(slot: str, gate=True, projects=2, milestones=10, coverage=1.0, tokens=100.0, infra=False):
        return {
            "slot": slot,
            "model": f"model/{slot}",
            "InstructionGate": gate,
            "ClosedLoopProjectCount": projects,
            "MilestoneStrictCount": milestones,
            "AcceptanceCoverage": coverage,
            "InferenceTokens": None if tokens is None else int(tokens * max(milestones, 1)),
            "InferenceTokensPerMilestoneStrictSuccess": tokens,
            "TokenMeasurementStatus": "indeterminate" if tokens is None else "valid",
            "CandidateTokenSoftSLA": tokens is not None,
            "DecisionInfrastructureIndeterminate": infra,
            "DecisionFinalist": False,
        }

    def test_build_rows_is_dynamic_and_strict_about_main_usage(self):
        manifest = load_manifest()
        evaluations, usages = complete_evaluations_and_usages(manifest)
        rows = build_rows(manifest, evaluations, usages)
        self.assertEqual(len(rows), len(manifest["candidate_slots"]))
        self.assertEqual(rows[0]["MilestoneStrictCount"], manifest["milestone_count"])
        bad = copy.deepcopy(usages)
        bad[0]["role"] = "main_agent"
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, bad)

    def test_build_rows_rejects_forged_tree_snapshot_metadata(self):
        manifest = load_manifest()
        evaluations, usages = complete_evaluations_and_usages(manifest)
        evaluations[0]["workspace_after"]["file_count"] = 1
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

        evaluations, usages = complete_evaluations_and_usages(manifest)
        evaluations[0]["workspace_before"]["tree_digest"] = "0" * 64
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

    def test_build_rows_rejects_empty_or_forged_nested_scores(self):
        manifest = load_manifest()
        evaluations, usages = complete_evaluations_and_usages(manifest)
        evaluations[0]["projects"] = []
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

        evaluations, usages = complete_evaluations_and_usages(manifest)
        evaluations[0]["ClosedLoopProjectCount"] -= 1
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

        evaluations, usages = complete_evaluations_and_usages(manifest)
        criterion = evaluations[0]["projects"][0]["milestones"][0]["criteria"][0]
        criterion["capability_id"] = "forged.capability"
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

        evaluations, usages = complete_evaluations_and_usages(manifest)
        criterion = evaluations[0]["projects"][0]["milestones"][0]["criteria"][0]
        criterion["status"] = "failed"
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

    def test_build_rows_recomputes_instruction_gate_from_observable_checks(self):
        manifest = load_manifest()
        evaluations, usages = complete_evaluations_and_usages(manifest)
        record = evaluations[0]
        record["InstructionGate"] = False
        record["instruction"]["instruction_gate"] = False
        record["ClosedLoopProjectCount"] = 0
        record["MilestoneStrictCount"] = 0
        for project in record["projects"]:
            project["closed_loop_success"] = False
            for milestone in project["milestones"]:
                milestone["strict_success"] = False
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

        evaluations, usages = complete_evaluations_and_usages(manifest)
        evaluations[0]["instruction"]["checks"].pop()
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

        evaluations, usages = complete_evaluations_and_usages(manifest)
        evaluations[0]["decision_infrastructure_indeterminate"] = True
        evaluations[0]["infrastructure_indeterminate_evidence"] = ["arbitrary"]
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

    def test_build_rows_rejects_suppressed_infrastructure_indeterminate_state(self):
        manifest = load_manifest()
        evaluations, usages = complete_evaluations_and_usages(manifest)
        record = evaluations[0]
        record["InstructionGate"] = None
        record["instruction"]["instruction_gate"] = None
        record["instruction"]["decision_infrastructure_indeterminate"] = True
        record["instruction"]["checks"][0]["status"] = "infrastructure_indeterminate"
        record["instruction"]["checks"][0]["passed"] = None
        record["ClosedLoopProjectCount"] = 0
        record["MilestoneStrictCount"] = 0
        for project in record["projects"]:
            project["closed_loop_success"] = False
            for milestone in project["milestones"]:
                milestone["strict_success"] = False
        with self.assertRaises(SchemaError):
            build_rows(manifest, evaluations, usages)

    def test_lexicographic_axis_order_dominates_lower_axes(self):
        manifest = load_manifest()
        rows = [
            self.row("subtest_1", projects=2, milestones=1, coverage=0.1, tokens=1000),
            self.row("subtest_2", projects=1, milestones=10, coverage=1.0, tokens=1),
        ]
        ranked = rank_rows(rows, manifest)
        self.assertEqual([row["slot"] for row in ranked], ["subtest_1", "subtest_2"])

    def test_final_axis_is_ascending_and_exact_full_axis_ties_share_rank(self):
        manifest = load_manifest()
        rows = [
            self.row("subtest_1", tokens=20),
            self.row("subtest_2", tokens=10),
            self.row("subtest_3", tokens=10),
        ]
        ranked = rank_rows(rows, manifest)
        self.assertEqual([row["slot"] for row in ranked], ["subtest_2", "subtest_3", "subtest_1"])
        self.assertEqual([row["Rank"] for row in ranked], [1, 1, 3])
        status, finalists, blockers = select_finalists(ranked)
        self.assertEqual(status, "exact_axis_tie")
        self.assertEqual(finalists, ["subtest_2", "subtest_3"])
        self.assertEqual(blockers, [])

    def test_decision_infrastructure_indeterminate_globally_blocks_finalists(self):
        rows = [self.row("subtest_1", tokens=10), self.row("subtest_2", projects=0, infra=True)]
        status, finalists, blockers = select_finalists(rows)
        self.assertEqual(status, "decision_infrastructure_indeterminate")
        self.assertEqual(finalists, [])
        self.assertTrue(blockers)

    def test_top_primary_set_missing_tokens_blocks_without_infinity_coercion(self):
        manifest = load_manifest()
        rows = [self.row("subtest_1", tokens=None), self.row("subtest_2", tokens=10)]
        ranked = rank_rows(rows, manifest)
        self.assertTrue(all(row["Rank"] is None for row in ranked))
        self.assertTrue(
            all(row["RankStatus"] == "indeterminate_within_primary_axes" for row in ranked)
        )
        status, finalists, blockers = select_finalists(ranked)
        self.assertEqual(status, "token_indeterminate")
        self.assertEqual(finalists, [])
        self.assertIn("subtest_1", blockers[0])


class LegacyAndComparisonTests(unittest.TestCase):
    def test_legacy_hash_audit_mechanically_checks_prototype_and_v4(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            prototype = project / "prototype"
            legacy_file = prototype / "legacy.txt"
            legacy_file.parent.mkdir(parents=True)
            legacy_file.write_text("legacy", encoding="utf-8")
            write_json(
                prototype / "results" / "audit.json",
                {
                    "files": {"legacy.txt": sha256_file(legacy_file)},
                    "workspace_hashes_after": {},
                },
            )
            v4 = prototype / "results" / "behavioral-quality-v4"
            v4.mkdir(parents=True)
            protocol = v4 / "protocol.md"
            probe = v4 / "probe-manifest.json"
            protocol.write_text("protocol", encoding="utf-8")
            probe.write_text("{}", encoding="utf-8")
            write_json(
                v4 / "audit.json",
                {
                    "input_hashes": {
                        "protocol.md": sha256_file(protocol),
                        "probe-manifest.json": sha256_file(probe),
                        "scripts": {},
                    },
                    "output_hashes": {},
                    "primary_results": {},
                    "workspace_trees": {},
                    "workspace_src_hashes": {},
                },
            )
            audit = audit_legacy_hashes(project)
            self.assertTrue(audit["passed"], audit["violations"])
            legacy_file.write_text("changed", encoding="utf-8")
            changed = audit_legacy_hashes(project)
            self.assertFalse(changed["passed"])
            self.assertTrue(any("prototype" in violation for violation in changed["violations"]))

    def test_top_set_movement_is_tie_aware(self):
        previous = {"round": "a", "top_set": ["s1", "s2"], "ranks": {"s1": 1, "s2": 1, "s3": 3}}
        current = {"round": "b", "top_set": ["s2", "s3"], "ranks": {"s1": 3, "s2": 1, "s3": 1}}
        movement = {item["slot"]: item for item in top_set_movement(previous, current, ["s1", "s2", "s3"])}
        self.assertEqual(movement["s1"]["top_set_movement"], "left_top_set")
        self.assertEqual(movement["s2"]["top_set_movement"], "remained_in_top_set")
        self.assertEqual(movement["s3"]["top_set_movement"], "entered_top_set")

    def test_pairwise_concordance_keeps_ties_and_indeterminate_separate(self):
        rounds = [
            {"round": "a", "ranks": {"s1": 1, "s2": 1, "s3": 3}},
            {"round": "b", "ranks": {"s1": 1, "s2": 2, "s3": 3}},
            {"round": "c", "ranks": {"s1": None, "s2": 2, "s3": 1}},
        ]
        result = pairwise_concordance(rounds, ["s1", "s2", "s3"])
        self.assertGreater(result["counts"]["tie_changed"], 0)
        self.assertGreater(result["counts"]["discordant"], 0)
        self.assertGreater(result["counts"]["indeterminate"], 0)
        self.assertEqual(pair_relation(rounds[0]["ranks"], "s1", "s2"), "tie")
        self.assertEqual(pair_relation(rounds[2]["ranks"], "s1", "s2"), "indeterminate")

    def test_sources_do_not_regress_to_task_count_literals_or_subjective_score(self):
        source_files = [path for path in HARNESS_ROOT.glob("*.py") if path.name != "__init__.py"]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
        self.assertNotIn("strict_success_count / 3", combined)
        self.assertNotIn("strict_count >= 2", combined)
        self.assertNotIn("subjective_score", combined.lower())
        self.assertEqual(
            RANKING_AXES,
            (
                ("InstructionGate", "desc"),
                ("ClosedLoopProjectCount", "desc"),
                ("MilestoneStrictCount", "desc"),
                ("AcceptanceCoverage", "desc"),
                ("InferenceTokensPerMilestoneStrictSuccess", "asc"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
