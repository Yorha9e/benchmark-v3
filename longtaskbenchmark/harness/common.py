from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence


CLOSED_LOOP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CLOSED_LOOP_ROOT.parent
PROTOTYPE_ROOT = PROJECT_ROOT / "prototype"
RUNTIME_EXCLUDED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})
RUNTIME_EXCLUDED_SUFFIXES = frozenset({".pyc"})
ISOLATED_ENV_REMOVED = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")
ALLOWED_SOURCE_PREFIXES = ("src/order_fulfillment/", "src/delivery_spool/")
RANKING_AXES = (
    ("InstructionGate", "desc"),
    ("ClosedLoopProjectCount", "desc"),
    ("MilestoneStrictCount", "desc"),
    ("AcceptanceCoverage", "desc"),
    ("InferenceTokensPerMilestoneStrictSuccess", "asc"),
)
SLOT_PATTERN = re.compile(r"subtest_[1-9][0-9]*\Z")
FINAL_REPORT_SCHEMA = b"""# Required final report schema

Return one final JSON object and no surrounding prose. It must have exactly these fields:

```json
{
  "schema_version": 1,
  "status": "completed|blocked",
  "projects": [
    {
      "id": "order_fulfillment|delivery_spool",
      "changed_files": ["workspace-relative/path"],
      "verification": [{"command": "exact command", "result": "pass|fail|not_run"}]
    }
  ],
  "constraints_respected": true,
  "unresolved": []
}
```

List both project objects exactly once. Paths must be workspace-relative. Do not claim a verification command ran unless it ran. A blocked delivery must use `status: "blocked"`, set `constraints_respected` from evidence, and explain every blocker in `unresolved`.
"""


class SchemaError(ValueError):
    """Raised when a benchmark-controlled record violates its schema."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_reject_duplicate_pairs)
    except json.JSONDecodeError as error:
        raise SchemaError(f"invalid JSON in {path}: {error}") from error


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SchemaError(f"cannot read JSONL {path}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise SchemaError(f"blank JSONL record at {path}:{line_number}")
        try:
            value = json.loads(line, object_pairs_hook=_reject_duplicate_pairs)
        except json.JSONDecodeError as error:
            raise SchemaError(f"invalid JSON at {path}:{line_number}: {error}") from error
        if not isinstance(value, dict):
            raise SchemaError(f"JSONL record at {path}:{line_number} is not an object")
        records.append(value)
    return records


def canonical_json_bytes(payload: Any, *, indent: int | None = None) -> bytes:
    separators = (",", ":") if indent is None else None
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=indent,
            separators=separators,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                temporary_name = None


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(payload, indent=2))


def atomic_write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    atomic_write_bytes(path, b"".join(canonical_json_bytes(dict(record)) for record in records))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ignored_relative_path(relative: Path) -> bool:
    return relative.suffix in RUNTIME_EXCLUDED_SUFFIXES or bool(
        RUNTIME_EXCLUDED_DIRS.intersection(relative.parts)
    )


def files_under(base: Path) -> Iterable[Path]:
    if not base.is_dir():
        return ()
    return (
        path
        for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix())
        if path.is_file() and not ignored_relative_path(path.relative_to(base))
    )


def tree_hashes(base: Path) -> dict[str, str]:
    return {path.relative_to(base).as_posix(): sha256_file(path) for path in files_under(base)}


def tree_digest(hashes: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(hashes), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return sha256_bytes(encoded)


def tree_snapshot(base: Path) -> dict[str, Any]:
    hashes = tree_hashes(base)
    return {"file_count": len(hashes), "tree_digest": tree_digest(hashes), "files": hashes}


def validate_tree_snapshot(value: Any, context: str = "tree snapshot") -> dict[str, Any]:
    snapshot = _expect_object(value, context)
    _expect_exact_keys(snapshot, {"file_count", "tree_digest", "files"}, set(), context)
    files = _expect_object(snapshot["files"], f"{context}.files")
    for relative, digest in files.items():
        path = Path(relative)
        if (
            not isinstance(relative, str)
            or not relative
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != relative
        ):
            raise SchemaError(f"{context} contains an unsafe or non-canonical path: {relative!r}")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SchemaError(f"{context} contains an invalid SHA-256 for {relative!r}")
    if not finite_nonnegative_integer(snapshot["file_count"]) or snapshot["file_count"] != len(files):
        raise SchemaError(f"{context} file_count does not match files")
    digest = snapshot["tree_digest"]
    if not isinstance(digest, str) or digest != tree_digest(files):
        raise SchemaError(f"{context} tree_digest does not match canonical files map")
    return snapshot


def changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def isolated_python_env(extra_python_paths: Sequence[Path] = ()) -> dict[str, str]:
    env = os.environ.copy()
    for key in ISOLATED_ENV_REMOVED:
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    if extra_python_paths:
        env["PYTHONPATH"] = os.pathsep.join(str(path.resolve()) for path in extra_python_paths)
    return env


def _expect_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{context} must be an object")
    return value


def _expect_exact_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], context: str
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing or unknown:
        raise SchemaError(f"{context} keys invalid; missing={missing!r}, unknown={unknown!r}")


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SchemaError(f"{context} must be a positive integer")
    return value


def validate_manifest(payload: Any) -> dict[str, Any]:
    manifest = _expect_object(payload, "manifest")
    _expect_exact_keys(
        manifest,
        {
            "schema_version",
            "benchmark_version",
            "status",
            "python_requirement",
            "random_seed",
            "candidate_slots",
            "waves",
            "resource_sla",
            "projects",
            "milestone_count",
            "criterion_count",
            "criterion_aggregation",
            "ranking_axes",
            "ties",
            "subjective_or_weighted_quality_score",
            "paths",
        },
        set(),
        "manifest",
    )
    if manifest["schema_version"] != 2 or manifest["benchmark_version"] != "closed-loop-v2":
        raise SchemaError("manifest schema_version/benchmark_version mismatch")
    if not isinstance(manifest["random_seed"], int) or isinstance(manifest["random_seed"], bool):
        raise SchemaError("manifest random_seed must be an integer")

    candidate_slots = _expect_object(manifest["candidate_slots"], "manifest.candidate_slots")
    if not candidate_slots:
        raise SchemaError("manifest.candidate_slots must not be empty")
    for slot, raw_config in candidate_slots.items():
        if not isinstance(slot, str) or SLOT_PATTERN.fullmatch(slot) is None:
            raise SchemaError(f"invalid candidate slot: {slot!r}")
        config = _expect_object(raw_config, f"manifest.candidate_slots.{slot}")
        _expect_exact_keys(config, {"model"}, {"thinking_effort"}, f"candidate {slot}")
        if not isinstance(config["model"], str) or not config["model"].strip():
            raise SchemaError(f"candidate {slot} model must be a non-empty string")
        if "thinking_effort" in config and (
            not isinstance(config["thinking_effort"], str) or not config["thinking_effort"].strip()
        ):
            raise SchemaError(f"candidate {slot} thinking_effort must be a non-empty string")

    waves = _expect_object(manifest["waves"], "manifest.waves")
    if not waves:
        raise SchemaError("manifest.waves must not be empty")
    flattened: list[str] = []
    for wave_name, members in waves.items():
        if not isinstance(wave_name, str) or not wave_name:
            raise SchemaError("wave names must be non-empty strings")
        if not isinstance(members, list) or not members:
            raise SchemaError(f"wave {wave_name} must be a non-empty list")
        if any(not isinstance(slot, str) for slot in members):
            raise SchemaError(f"wave {wave_name} contains a non-string slot")
        flattened.extend(members)
    if len(flattened) != len(set(flattened)) or set(flattened) != set(candidate_slots):
        raise SchemaError("waves must contain every candidate slot exactly once")

    resource_sla = _expect_object(manifest["resource_sla"], "manifest.resource_sla")
    _expect_exact_keys(
        resource_sla,
        {"candidate_inference_tokens_soft", "main_agent_usage_excluded"},
        set(),
        "manifest.resource_sla",
    )
    _positive_int(resource_sla["candidate_inference_tokens_soft"], "candidate token SLA")
    if resource_sla["main_agent_usage_excluded"] is not True:
        raise SchemaError("main_agent_usage_excluded must be true")

    projects = manifest["projects"]
    if not isinstance(projects, list) or not projects:
        raise SchemaError("manifest.projects must be a non-empty list")
    project_ids: list[str] = []
    milestone_ids: list[str] = []
    for index, raw_project in enumerate(projects):
        project = _expect_object(raw_project, f"manifest.projects[{index}]")
        _expect_exact_keys(project, {"id", "milestones"}, set(), f"manifest.projects[{index}]")
        project_id = project["id"]
        milestones = project["milestones"]
        if not isinstance(project_id, str) or not project_id:
            raise SchemaError("project id must be a non-empty string")
        if not isinstance(milestones, list) or not milestones or any(
            not isinstance(item, str) or not item for item in milestones
        ):
            raise SchemaError(f"project {project_id} milestones must be non-empty strings")
        project_ids.append(project_id)
        milestone_ids.extend(milestones)
    if len(project_ids) != len(set(project_ids)) or len(milestone_ids) != len(set(milestone_ids)):
        raise SchemaError("project and milestone IDs must be unique")
    if manifest["milestone_count"] != len(milestone_ids):
        raise SchemaError("manifest milestone_count does not match projects")
    _positive_int(manifest["criterion_count"], "manifest criterion_count")

    axes = manifest["ranking_axes"]
    if not isinstance(axes, list):
        raise SchemaError("manifest.ranking_axes must be a list")
    parsed_axes: list[tuple[str, str]] = []
    for index, raw_axis in enumerate(axes):
        axis = _expect_object(raw_axis, f"manifest.ranking_axes[{index}]")
        _expect_exact_keys(axis, {"id", "direction"}, set(), f"ranking axis {index}")
        parsed_axes.append((axis["id"], axis["direction"]))
    if tuple(parsed_axes) != RANKING_AXES:
        raise SchemaError(f"ranking axes must be exactly {RANKING_AXES!r}")
    if manifest["ties"] != "preserve_when_all_axes_equal":
        raise SchemaError("manifest tie rule mismatch")
    if manifest["subjective_or_weighted_quality_score"] is not False:
        raise SchemaError("subjective_or_weighted_quality_score must be false")

    paths = _expect_object(manifest["paths"], "manifest.paths")
    _expect_exact_keys(paths, {"frozen_plan", "briefing", "asset_hashes"}, set(), "manifest.paths")
    for name, value in paths.items():
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise SchemaError(f"manifest path {name} must be a safe relative path")
    return manifest


def load_manifest(root: Path = CLOSED_LOOP_ROOT) -> dict[str, Any]:
    return validate_manifest(load_json(root / "manifest.json"))


def validate_criteria(payload: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    criteria = _expect_object(payload, "criteria")
    _expect_exact_keys(
        criteria,
        {"schema_version", "criterion_count", "milestone_count", "aggregation", "projects"},
        set(),
        "criteria",
    )
    if criteria["schema_version"] != manifest["schema_version"]:
        raise SchemaError("criteria schema_version does not match manifest")
    if criteria["criterion_count"] != manifest["criterion_count"]:
        raise SchemaError("criteria criterion_count does not match manifest")
    if criteria["milestone_count"] != manifest["milestone_count"]:
        raise SchemaError("criteria milestone_count does not match manifest")
    if not isinstance(criteria["projects"], list):
        raise SchemaError("criteria.projects must be a list")

    expected_projects = {project["id"]: project["milestones"] for project in manifest["projects"]}
    seen_projects: list[str] = []
    seen_milestones: list[str] = []
    seen_criteria: list[str] = []
    for project_index, raw_project in enumerate(criteria["projects"]):
        project = _expect_object(raw_project, f"criteria.projects[{project_index}]")
        _expect_exact_keys(project, {"id", "milestones"}, set(), f"criteria project {project_index}")
        project_id = project["id"]
        if project_id not in expected_projects:
            raise SchemaError(f"unknown criteria project {project_id!r}")
        milestones = project["milestones"]
        if not isinstance(milestones, list):
            raise SchemaError(f"criteria project {project_id} milestones must be a list")
        project_milestones: list[str] = []
        for milestone_index, raw_milestone in enumerate(milestones):
            milestone = _expect_object(raw_milestone, f"criteria milestone {milestone_index}")
            _expect_exact_keys(
                milestone, {"id", "capability_id", "criteria"}, set(), f"criteria milestone {milestone_index}"
            )
            milestone_id = milestone["id"]
            milestone_capability = milestone["capability_id"]
            if not isinstance(milestone_id, str) or not milestone_id:
                raise SchemaError("milestone id must be a non-empty string")
            if not isinstance(milestone_capability, str) or not milestone_capability:
                raise SchemaError(f"milestone {milestone_id} capability_id must be a non-empty string")
            members = milestone["criteria"]
            if not isinstance(members, list) or not members:
                raise SchemaError(f"milestone {milestone_id} criteria must be a non-empty list")
            project_milestones.append(milestone_id)
            seen_milestones.append(milestone_id)
            for criterion_index, raw_criterion in enumerate(members):
                criterion = _expect_object(raw_criterion, f"criterion {criterion_index}")
                _expect_exact_keys(
                    criterion, {"id", "capability_id", "unittest"}, set(), f"criterion {criterion_index}"
                )
                criterion_id = criterion["id"]
                criterion_capability = criterion["capability_id"]
                test_name = criterion["unittest"]
                if not isinstance(criterion_id, str) or not criterion_id:
                    raise SchemaError("criterion id must be a non-empty string")
                if not isinstance(criterion_capability, str) or not criterion_capability:
                    raise SchemaError(f"criterion {criterion_id} capability_id must be a non-empty string")
                if not isinstance(test_name, str) or not test_name.startswith("closed_loop_v2.evaluator."):
                    raise SchemaError(f"criterion {criterion_id} has invalid unittest path")
                seen_criteria.append(criterion_id)
        if project_milestones != expected_projects[project_id]:
            raise SchemaError(f"criteria milestones for {project_id} do not match manifest order")
        seen_projects.append(project_id)
    if seen_projects != list(expected_projects):
        raise SchemaError("criteria projects do not match manifest order")
    if len(seen_milestones) != len(set(seen_milestones)) or len(seen_milestones) != criteria["milestone_count"]:
        raise SchemaError("criteria milestone IDs/count are invalid")
    if len(seen_criteria) != len(set(seen_criteria)) or len(seen_criteria) != criteria["criterion_count"]:
        raise SchemaError("criteria IDs/count are invalid")
    return criteria


def load_criteria(root: Path = CLOSED_LOOP_ROOT, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    actual_manifest = manifest or load_manifest(root)
    return validate_criteria(load_json(root / "evaluator" / "criteria.json"), actual_manifest)


def ordered_slots(manifest: Mapping[str, Any]) -> list[str]:
    return list(manifest["candidate_slots"])


def expected_model(manifest: Mapping[str, Any], slot: str) -> str:
    candidates = manifest["candidate_slots"]
    if slot not in candidates:
        raise SchemaError(f"unknown slot: {slot!r}")
    return candidates[slot]["model"]


def wave_for_slot(manifest: Mapping[str, Any], slot: str) -> str:
    expected_model(manifest, slot)
    return next(name for name, members in manifest["waves"].items() if slot in members)


def validate_slot_model(manifest: Mapping[str, Any], slot: Any, model: Any) -> None:
    if not isinstance(slot, str) or slot not in manifest["candidate_slots"]:
        raise SchemaError(f"unknown slot: {slot!r}")
    expected = manifest["candidate_slots"][slot]["model"]
    if model != expected:
        raise SchemaError(f"slot {slot} model={model!r}, expected {expected!r}")


def validate_agent_map(payload: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    mapping = _expect_object(payload, "agent map")
    _expect_exact_keys(mapping, {"schema_version", "executors"}, {"main_agent", "planner"}, "agent map")
    if mapping["schema_version"] != manifest["schema_version"]:
        raise SchemaError("agent map schema_version mismatch")
    executors = mapping["executors"]
    if not isinstance(executors, list):
        raise SchemaError("agent map executors must be a list")
    seen: list[str] = []
    replacement_fields = {
        "replacement_for_agent_id",
        "previous_failure_class",
        "previous_failure_reason",
        "previous_model_response_started",
    }
    for index, raw_executor in enumerate(executors):
        executor = _expect_object(raw_executor, f"agent map executor[{index}]")
        slot = executor.get("slot")
        candidate = manifest["candidate_slots"].get(slot) if isinstance(slot, str) else None
        required = {"slot", "agent_id", "model", "status", "wave"}
        if isinstance(candidate, dict) and "thinking_effort" in candidate:
            required.add("thinking_effort")
        optional = {"retry_count", *replacement_fields}
        _expect_exact_keys(executor, required, optional, f"agent map executor[{index}]")
        validate_slot_model(manifest, executor["slot"], executor["model"])
        if not isinstance(executor["agent_id"], str) or not executor["agent_id"].strip():
            raise SchemaError(f"executor {executor['slot']} has invalid agent_id")
        if not isinstance(executor["status"], str) or not executor["status"]:
            raise SchemaError(f"executor {executor['slot']} has invalid status")
        if executor["wave"] != wave_for_slot(manifest, executor["slot"]):
            raise SchemaError(f"executor {executor['slot']} wave does not match manifest")
        expected_effort = manifest["candidate_slots"][executor["slot"]].get("thinking_effort")
        if expected_effort is not None and executor["thinking_effort"] != expected_effort:
            raise SchemaError(f"executor {executor['slot']} thinking_effort does not match manifest")
        if expected_effort is None and "thinking_effort" in executor:
            raise SchemaError(f"executor {executor['slot']} must not declare thinking_effort")

        retry_count = executor.get("retry_count", 0)
        if not finite_nonnegative_integer(retry_count) or retry_count > 1:
            raise SchemaError(
                f"executor {executor['slot']} retry_count must be 0 or 1; the schema carries one prior-attempt record"
            )
        present_replacement = replacement_fields.intersection(executor)
        if retry_count == 0 and present_replacement:
            raise SchemaError(f"executor {executor['slot']} has replacement evidence without a retry")
        if retry_count > 0:
            required_replacement = {"replacement_for_agent_id", "previous_model_response_started"}
            if not required_replacement.issubset(executor):
                raise SchemaError(f"executor {executor['slot']} retry lacks replacement evidence")
            replaced = executor["replacement_for_agent_id"]
            if not isinstance(replaced, str) or not replaced.strip() or replaced == executor["agent_id"]:
                raise SchemaError(f"executor {executor['slot']} has invalid replacement_for_agent_id")
            if executor["previous_model_response_started"] is not False:
                raise SchemaError(
                    f"executor {executor['slot']} previous response started; current wire alone cannot account for tokens"
                )
            for field in ("previous_failure_class", "previous_failure_reason"):
                if field in executor and (
                    not isinstance(executor[field], str) or not executor[field].strip()
                ):
                    raise SchemaError(f"executor {executor['slot']} has invalid {field}")
        seen.append(executor["slot"])
    if len(seen) != len(set(seen)) or set(seen) != set(manifest["candidate_slots"]):
        raise SchemaError("agent map must contain every slot exactly once")
    for role in ("main_agent", "planner"):
        if role in mapping:
            record = _expect_object(mapping[role], f"agent map {role}")
            _expect_exact_keys(record, {"agent_id", "model", "status"}, set(), f"agent map {role}")
    return mapping


def validation_expectations(
    root: Path, criteria_manifest: Mapping[str, Any]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    expected_criteria = [
        {"id": criterion["id"], "unittest": criterion["unittest"]}
        for project in criteria_manifest["projects"]
        for milestone in project["milestones"]
        for criterion in milestone["criteria"]
    ]
    mutant_document = _expect_object(
        load_json(root / "validation" / "mutants" / "manifest.json"),
        "validation mutant manifest",
    )
    _expect_exact_keys(
        mutant_document,
        {"schema_version", "mutants"},
        set(),
        "validation mutant manifest",
    )
    mutants = mutant_document["mutants"]
    if mutant_document["schema_version"] != 1 or not isinstance(mutants, list) or len(mutants) < 10:
        raise SchemaError("validation mutant manifest is invalid")
    criterion_ids = {item["id"] for item in expected_criteria}
    expected_mutants: list[dict[str, str]] = []
    for index, raw_mutant in enumerate(mutants):
        mutant = _expect_object(raw_mutant, f"validation mutant[{index}]")
        _expect_exact_keys(
            mutant,
            {"id", "target_criterion", "source", "replacements"},
            set(),
            f"validation mutant[{index}]",
        )
        mutant_id = mutant["id"]
        target = mutant["target_criterion"]
        if (
            not isinstance(mutant_id, str)
            or not mutant_id
            or not isinstance(target, str)
            or target not in criterion_ids
        ):
            raise SchemaError(f"validation mutant[{index}] id/target is invalid")
        expected_mutants.append({"id": mutant_id, "target_criterion": target})
    if len({item["id"] for item in expected_mutants}) != len(expected_mutants):
        raise SchemaError("validation mutant ids are not unique")
    return expected_criteria, expected_mutants


def validate_reference_validation_summary(
    payload: Any,
    expected_criteria: Sequence[Mapping[str, str]],
    expected_mutants: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    summary = _expect_object(payload, "reference validation summary")
    _expect_exact_keys(
        summary,
        {
            "reference_runs",
            "normalized_consistent",
            "criteria",
            "reference_score",
            "mutants",
            "overall_passed",
        },
        set(),
        "reference validation summary",
    )
    if len(expected_criteria) != 20 or len(expected_mutants) < 10:
        raise SchemaError("validation expectations are incomplete")
    criteria = _expect_object(summary["criteria"], "reference validation criteria")
    _expect_exact_keys(criteria, {"passed", "total"}, set(), "reference validation criteria")
    if (
        summary["overall_passed"] is not True
        or summary["normalized_consistent"] is not True
        or criteria != {"passed": 20, "total": 20}
        or summary["reference_score"] != "20/20"
    ):
        raise SchemaError("reference validation did not establish consistent 20/20 behavior")
    reference_runs = summary["reference_runs"]
    if not isinstance(reference_runs, list) or len(reference_runs) != 2:
        raise SchemaError("reference validation must contain exactly two runs")
    normalized_criteria: list[list[dict[str, Any]]] = []
    criterion_keys = {"criterion", "target", "status", "passed", "executed", "tests_run", "returncode"}
    for index, raw_run in enumerate(reference_runs, start=1):
        run = _expect_object(raw_run, f"reference validation run {index}")
        _expect_exact_keys(run, {"run", "passed", "total", "criteria"}, set(), f"reference validation run {index}")
        if run["run"] != index or run["passed"] != 20 or run["total"] != 20:
            raise SchemaError(f"reference validation run {index} is not 20/20")
        evidence = run["criteria"]
        if not isinstance(evidence, list) or len(evidence) != len(expected_criteria):
            raise SchemaError(f"reference validation run {index} criterion evidence is incomplete")
        for position, (item, expected) in enumerate(zip(evidence, expected_criteria)):
            if not isinstance(item, dict) or set(item) != criterion_keys:
                raise SchemaError(f"reference validation run {index} criterion[{position}] schema mismatch")
            if (
                item["criterion"] != expected["id"]
                or item["target"] != expected["unittest"]
                or item["status"] != "passed"
                or item["passed"] is not True
                or item["executed"] is not True
                or item["tests_run"] != 1
                or item["returncode"] != 0
            ):
                raise SchemaError(f"reference validation run {index} criterion[{position}] is invalid")
        normalized_criteria.append(evidence)
    if normalized_criteria[0] != normalized_criteria[1]:
        raise SchemaError("reference validation runs are not normalized-consistent")

    mutants = summary["mutants"]
    if not isinstance(mutants, list) or len(mutants) != len(expected_mutants):
        raise SchemaError("reference validation mutant evidence is incomplete")
    mutant_keys = {"id", "criterion", "detected", "status", "diagnostic"}
    for index, (mutant, expected) in enumerate(zip(mutants, expected_mutants)):
        if not isinstance(mutant, dict) or set(mutant) != mutant_keys:
            raise SchemaError(f"reference validation mutant[{index}] schema mismatch")
        if (
            mutant["id"] != expected["id"]
            or mutant["criterion"] != expected["target_criterion"]
            or mutant["detected"] is not True
            or mutant["status"] != "detected"
            or not isinstance(mutant["diagnostic"], str)
        ):
            raise SchemaError(f"reference validation mutant[{index}] was not detected as expected")
    return summary


def build_briefing_bytes(root: Path = CLOSED_LOOP_ROOT) -> bytes:
    manifest = load_manifest(root)
    plan_path = root / manifest["paths"]["frozen_plan"]
    tasks_path = root / "template" / "TASKS.md"
    constraints_path = root / "template" / "BRIEFING.preamble.md"
    missing = [path for path in (constraints_path, tasks_path, plan_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError("briefing inputs missing: " + ", ".join(str(path) for path in missing))
    constraints = constraints_path.read_bytes()
    tasks = tasks_path.read_bytes()
    plan = plan_path.read_bytes()
    return (
        constraints
        + b"\n\n# Frozen task contract\n\n"
        + tasks
        + b"\n\n# Frozen execution plan\n\n"
        + plan
        + b"\n\n"
        + FINAL_REPORT_SCHEMA
    )


def is_allowed_source_path(relative: str) -> bool:
    return any(relative.startswith(prefix) for prefix in ALLOWED_SOURCE_PREFIXES)


def verify_workspace_non_source(workspace: Path, root: Path = CLOSED_LOOP_ROOT) -> list[str]:
    template_hashes = tree_hashes(root / "template")
    workspace_hash_map = tree_hashes(workspace)
    expected = {path: digest for path, digest in template_hashes.items() if not is_allowed_source_path(path)}
    actual = {path: digest for path, digest in workspace_hash_map.items() if not is_allowed_source_path(path)}
    violations: list[str] = []
    for relative, digest in expected.items():
        if relative not in actual:
            violations.append(f"missing workspace asset: {relative}")
        elif actual[relative] != digest:
            violations.append(f"changed workspace asset: {relative}")
    for relative in sorted(set(actual) - set(expected)):
        violations.append(f"unauthorized workspace file: {relative}")
    for relative in workspace_hash_map:
        if relative.startswith("src/") and not is_allowed_source_path(relative):
            violations.append(f"source outside allowed package boundaries: {relative}")
    return sorted(set(violations))


def validate_asset_manifest(payload: Any) -> dict[str, Any]:
    manifest = _expect_object(payload, "asset hash manifest")
    _expect_exact_keys(
        manifest,
        {"schema_version", "benchmark_version", "generated_at", "files"},
        set(),
        "asset hash manifest",
    )
    if manifest["schema_version"] != 2 or manifest["benchmark_version"] != "closed-loop-v2":
        raise SchemaError("asset hash manifest version mismatch")
    files = _expect_object(manifest["files"], "asset hash manifest files")
    if not files:
        raise SchemaError("asset hash manifest files must not be empty")
    for relative, digest in files.items():
        path = Path(relative)
        if (
            not isinstance(relative, str)
            or not relative
            or path.is_absolute()
            or ".." in path.parts
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise SchemaError(f"invalid asset hash entry: {relative!r}")
    return manifest


def verify_assets(root: Path = CLOSED_LOOP_ROOT, asset_manifest: Path | None = None) -> list[str]:
    manifest_path = asset_manifest or root / "asset-hashes.json"
    if not manifest_path.is_file():
        return [f"missing asset manifest: {manifest_path}"]
    try:
        manifest = validate_asset_manifest(load_json(manifest_path))
    except (OSError, SchemaError) as error:
        return [f"invalid asset manifest: {error}"]
    violations: list[str] = []
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.is_file():
            violations.append(f"missing asset: {relative}")
        elif sha256_file(path) != expected:
            violations.append(f"changed asset: {relative}")
    return violations


def finite_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
