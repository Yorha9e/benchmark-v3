"""Self-test the external reviewer evaluator without modifying candidates."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Dict, Sequence

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHORT = ROOT / "short"
PACKAGE = pathlib.Path(__file__).resolve().parent
CHECKS = PACKAGE / "checks.py"
OFFICIAL = SHORT / "harness" / "evaluate.py"
SCHEMA = PACKAGE / "review_schema.json"

try:
    from . import EVALUATOR_VERSION
    from . import checks, rank, runner
except ImportError:  # direct execution by an absolute script path
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from reviewer_evaluator import EVALUATOR_VERSION  # type: ignore
    from reviewer_evaluator import checks, rank, runner  # type: ignore


def _last_json(completed: subprocess.CompletedProcess[str]) -> Any:
    if completed.returncode != 0 or not completed.stdout.strip():
        raise AssertionError(completed.stderr[-500:] or "subprocess emitted no JSON")
    text = completed.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text.splitlines()[-1])


def _subprocess_json(
    command: list[str], cwd: pathlib.Path, timeout: float = 180
) -> Any:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
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
    return _last_json(completed)


def _subprocess_raw(
    command: list[str], cwd: pathlib.Path, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
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


def _assert_check_result(item: dict, check_id: str | None = None) -> None:
    required = {"id", "passed", "duration_ms", "error_type", "message"}
    assert required <= set(item)
    if check_id is not None:
        assert item["id"] == check_id
    assert isinstance(item["id"], str)
    assert isinstance(item["passed"], bool)
    assert isinstance(item["duration_ms"], (int, float))
    if item["passed"]:
        assert item["error_type"] is None


def _assert_structured_checks(value: dict, expected_total: int = 8) -> None:
    assert isinstance(value, dict)
    results = value.get("checks")
    assert isinstance(results, list) and len(results) == expected_total
    for item in results:
        _assert_check_result(item)


def _reference_official() -> dict:
    result = _subprocess_json(
        [
            sys.executable,
            "-I",
            str(OFFICIAL),
            "--workspace",
            str(SHORT),
            "--condition",
            "A",
            "--slot",
            "reference",
        ],
        SHORT / "harness",
    )
    manifest = json.loads((SHORT / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    assert result.get("instruction_gate") is True
    assert result.get("strict", {}).get("OfficialCriterionCount") == sum(
        len(task["criteria"]) for task in manifest["tasks"]
    )
    assert result.get("strict", {}).get("StrictTaskCount") == len(
        manifest["tasks"]
    )
    assert all(item.get("passed") for item in result.get("criteria", []))
    assert all(
        item.get("passed")
        for values in result.get("capabilities", {}).values()
        for item in values
    )
    return result


def _copy_reference_candidate(parent: pathlib.Path, candidate_id: str) -> pathlib.Path:
    candidate = parent / candidate_id
    shutil.copytree(SHORT / "template", candidate)
    shutil.copy2(SHORT / "TASKS.md", candidate / "TASKS.md")
    reference = SHORT / "validation" / "reference"
    manifest = json.loads((SHORT / "manifest.json").read_text(encoding="utf-8"))
    for task in manifest["tasks"]:
        source = reference / "solutions" / task["file"]
        if task["file"] == "dependency_layers.py":
            source = ROOT / "reviewer" / "solutions" / task["file"]
        shutil.copy2(source, candidate / "solutions" / task["file"])
    shutil.copy2(reference / "instruction_ack.json", candidate / "instruction_ack.json")
    (candidate / "cell.json").write_text(
        json.dumps(
            {
                "benchmark_id": "short-python-stdlib-v1",
                "condition": "B",
                "slot": "B11",
                "expected_model": "volcano/minimax-m3",
                "group": "G2",
                "wave": "G2-B",
                "public_inputs": ["TASKS.md", "public_smoke_tests.py"],
            }
        ),
        encoding="utf-8",
    )
    return candidate


def _assert_candidate_isolation(candidate: pathlib.Path) -> None:
    forbidden = {"reviewer_evaluator", "evaluator", "harness"}
    for path in candidate.rglob("*"):
        if any(part in forbidden or "reviewer_evaluator" in part for part in path.parts):
            raise AssertionError(f"evaluator file leaked into candidate workspace: {path}")


def _targeted_mutants() -> Dict[str, SimpleNamespace]:
    """Return one obvious behavior mutant for each supplemental criterion."""
    class DependencyCycleError(ValueError):
        def __init__(self, nodes=()):
            self.nodes = tuple(nodes)
            super().__init__("mutant cycle")

    def cross_mutant(edges):
        pairs = list(edges)
        return [["root"], [pairs[0][0], pairs[1][0]]]

    def collision_mutant(edges):
        pairs = list(edges)
        return [["root"], [pairs[0][0]]]

    def unhashable_mutant(edges):
        pairs = list(edges)
        return [[["root"]], [pairs[0][0], pairs[1][0]]]

    def cycle_mutant(edges):
        list(edges)
        return []

    def stable_mutant(edges):
        list(edges)
        return [["root-a", "root-b"], ["early", "late"], ["join"]]

    def one_shot_mutant(edges):
        list(edges)
        list(edges)
        return [["root"], ["child"]]

    def deep_mutant(edges):
        list(edges)
        raise RecursionError("recursive mutant")

    def wide_mutant(edges):
        list(edges)
        return [[0], []]

    functions = {
        "cross_hashability": cross_mutant,
        "hash_collision": collision_mutant,
        "unhashable_equal_dedup": unhashable_mutant,
        "cycle_blocked_and_peeled": cycle_mutant,
        "stable_multi_parent_layers": stable_mutant,
        "one_shot_generator": one_shot_mutant,
        "deep_50000": deep_mutant,
        "wide_50000": wide_mutant,
    }
    return {
        check_id: SimpleNamespace(
            dependency_layers=callback, DependencyCycleError=DependencyCycleError
        )
        for check_id, callback in functions.items()
    }


def _assert_mutants() -> int:
    caught = 0
    mutants = _targeted_mutants()
    assert set(mutants) == set(checks.CHECK_IDS)
    for check_id in checks.CHECK_IDS:
        result = checks.run_one(mutants[check_id], check_id)
        _assert_check_result(result, check_id)
        assert result["passed"] is False, f"mutant escaped: {check_id}"
        caught += 1
    return caught


def _assert_timeout_isolation(candidate: pathlib.Path) -> None:
    solution = candidate / "solutions" / "dependency_layers.py"
    source = solution.read_text(encoding="utf-8")
    prefix = (
        "import sys as _sys, time as _time\n"
        "if '--check' in _sys.argv and "
        "_sys.argv[_sys.argv.index('--check') + 1] == 'cross_hashability':\n"
        "    _time.sleep(2)\n"
    )
    solution.write_text(prefix + source, encoding="utf-8")
    result = runner._aggregate_supplemental(solution, timeout=1.0)
    _assert_structured_checks(result)
    by_id = {item["id"]: item for item in result["checks"]}
    assert by_id["cross_hashability"]["passed"] is False
    assert by_id["cross_hashability"]["error_type"] == "TimeoutError"
    assert all(
        item["passed"] for check_id, item in by_id.items()
        if check_id != "cross_hashability"
    )


def _ranking_run(
    candidate_id: str,
    gate: int,
    raw: int,
    resource: int = 1,
    supplemental: int = 8,
    performance: float = 10.0,
    tokens: int = 100,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "official": {
            "instruction_gate": bool(gate),
            "strict": {
                "InstructionGate": gate,
                "ResourceCapabilityCount": resource,
            },
            "lenient": {"RawCriterionCount": raw},
        },
        "supplemental": {
            "passed": supplemental,
            "performance_median_ms": performance,
        },
        "usage": {"available": True, "total_tokens": tokens},
    }


def _assert_ranking() -> None:
    result = rank.rank(
        [
            _ranking_run("behavior-best", gate=0, raw=99),
            _ranking_run("gate-pass", gate=1, raw=1),
            _ranking_run("tie-b", gate=1, raw=5),
            _ranking_run("tie-a", gate=1, raw=5),
        ]
    )
    strict = result["strict_deterministic_rank"]
    lenient = result["lenient_deterministic_rank"]
    assert strict[-1]["candidate_id"] == "behavior-best"
    assert lenient[0]["candidate_id"] == "behavior-best"

    strict_ties = {item["candidate_id"]: item["rank"] for item in strict}
    lenient_ties = {item["candidate_id"]: item["rank"] for item in lenient}
    assert strict_ties["tie-a"] == strict_ties["tie-b"]
    assert lenient_ties["tie-a"] == lenient_ties["tie-b"]
    assert ["tie-a", "tie-b"] in result["unresolved_ties"]["strict"]
    assert ["tie-a", "tie-b"] in result["unresolved_ties"]["lenient"]


def _assert_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    evidence = schema["properties"]["evidence"]
    assert evidence["type"] == "array" and evidence["minItems"] == 1
    assert evidence["items"]["type"] == "object"
    assert set(evidence["items"]["required"]) == {
        "file", "line_start", "line_end", "reason"
    }
    assert evidence["items"]["additionalProperties"] is False
    assert schema["properties"]["confidence"]["enum"] == [
        "high", "medium", "low"
    ]


def _assert_rank_output(parent: pathlib.Path) -> None:
    input_path = parent / "rank-input.json"
    output_path = parent / "rank-output.json"
    input_path.write_text(
        json.dumps([_ranking_run("rank-candidate", gate=1, raw=8)]),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-B",
        "-I",
        str(PACKAGE / "rank.py"),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    first = _subprocess_json(command, ROOT)
    assert "strict_deterministic_rank" in first
    assert output_path.is_file()
    assert json.loads(output_path.read_text(encoding="utf-8")) == first
    second = _subprocess_raw(command, ROOT)
    assert second.returncode == 2
    assert "refusing to overwrite" in second.stderr


def run_self_test() -> dict:
    """Run all evaluator protocol invariants and return a compact report."""
    official = _reference_official()
    reference_solution = ROOT / "reviewer" / "solutions" / "dependency_layers.py"
    first = _subprocess_json(
        [sys.executable, "-I", str(CHECKS), str(reference_solution)], ROOT
    )
    second = _subprocess_json(
        [sys.executable, "-I", str(CHECKS), str(reference_solution)], ROOT
    )
    _assert_structured_checks(first)
    _assert_structured_checks(second)
    assert first["passed"] == 8 and second["passed"] == 8
    assert [item["passed"] for item in first["checks"]] == [
        item["passed"] for item in second["checks"]
    ]

    for check_id in checks.CHECK_IDS:
        single = _subprocess_json(
            [
                sys.executable,
                "-I",
                str(CHECKS),
                str(reference_solution),
                "--check",
                check_id,
            ],
            ROOT,
        )
        _assert_check_result(single, check_id)
        assert single["passed"] is True

    mutants_caught = _assert_mutants()
    _assert_ranking()
    _assert_schema()

    with tempfile.TemporaryDirectory(
        prefix="reviewer-evaluator-selftest-", dir=str(ROOT)
    ) as temporary:
        parent = pathlib.Path(temporary)
        direct = _copy_reference_candidate(parent, "direct-workspace")
        child = _copy_reference_candidate(parent, "candidate-beta")
        timeout_candidate = _copy_reference_candidate(parent, "timeout-candidate")
        _assert_candidate_isolation(direct)
        _assert_candidate_isolation(child)

        direct_resolved = runner._resolve_candidate("candidate-alpha", direct)
        child_resolved = runner._resolve_candidate("candidate-beta", parent)
        assert direct_resolved[2] == "candidate-alpha"
        assert child_resolved[0] == child and child_resolved[2] == "candidate-beta"
        assert direct_resolved[3].get("slot") == "B11"

        report = runner.evaluate("self-test", "candidate-alpha", direct)
        assert report["candidate_id"] == "candidate-alpha"
        assert report["binding_slot"] == "B11"
        assert report["evaluator_version"] == EVALUATOR_VERSION
        assert report["official"].get("instruction_gate") is True
        assert report["supplemental"].get("passed") == 8
        assert report["usage"].get("available") is False

        cell_path = direct / "cell.json"
        cell_before = cell_path.read_text(encoding="utf-8")
        override_output = parent / "override-output.json"
        assert runner.main([
            "--run", "override-self-test",
            "--candidate", "candidate-alpha",
            "--workspace", str(direct),
            "--output", str(override_output),
            "--model", "override-model",
            "--binding-slot", "override-slot",
        ]) == 0
        override_report = json.loads(override_output.read_text(encoding="utf-8"))
        assert override_report["candidate_id"] == "candidate-alpha"
        assert override_report["model"] == "override-model"
        assert override_report["binding_slot"] == "override-slot"
        assert cell_path.read_text(encoding="utf-8") == cell_before

        _assert_timeout_isolation(timeout_candidate)

        missing_report = runner.evaluate("missing", "missing-candidate", parent)
        assert missing_report["candidate_id"] == "missing-candidate"
        assert missing_report["official"]["passed"] is False
        _assert_structured_checks(missing_report["supplemental"])

        broken = parent / "broken.py"
        broken.write_text("raise RuntimeError('broken candidate')\n", encoding="utf-8")
        broken_result = _subprocess_json(
            [sys.executable, "-I", str(CHECKS), str(broken)], ROOT
        )
        _assert_structured_checks(broken_result)
        assert broken_result["passed"] == 0

        _assert_rank_output(parent)
        output = parent / "output.json"
        runner.atomic_json(output, report)
        try:
            runner.atomic_json(output, report)
        except FileExistsError:
            pass
        else:
            raise AssertionError("output overwrite was not rejected")
        assert not (direct / "reviewer_evaluator").exists()

    return {
        "status": "pass",
        "evaluator_version": EVALUATOR_VERSION,
        "official_reference_criteria": official["strict"]["OfficialCriterionCount"],
        "supplemental_reference_checks": 8,
        "mutants_caught": mutants_caught,
        "checks": [
            "candidate_identity",
            "criterion_subprocess_isolation",
            "repeatability",
            "missing_candidate",
            "exception",
            "timeout_isolation",
            "no_overwrite",
            "candidate_isolation",
            "strict_lenient_ranking",
            "review_schema",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        print(json.dumps(run_self_test(), sort_keys=True, ensure_ascii=False))
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"evaluator self-test failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
