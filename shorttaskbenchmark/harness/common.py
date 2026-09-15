"""Shared deterministic helpers for the short benchmark harness."""

import hashlib
import json
import os
import pathlib
import sys
import tempfile


def benchmark_root():
    return pathlib.Path(__file__).resolve().parents[1]


def load_manifest(root=None):
    root = pathlib.Path(root) if root else benchmark_root()
    with (root / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("manifest tasks must be a non-empty list")
    task_ids = [task.get("id") for task in tasks]
    if any(not isinstance(item, str) for item in task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("task ids must be unique strings")
    files = [task.get("file") for task in tasks]
    if any(not isinstance(item, str) for item in files) or len(set(files)) != len(files):
        raise ValueError("task files must be unique strings")
    criteria = [criterion for task in tasks for criterion in task.get("criteria", [])]
    if not criteria or any(not isinstance(item, str) for item in criteria) or len(set(criteria)) != len(criteria):
        raise ValueError("criterion ids must be present and unique")
    conditions = manifest.get("conditions")
    if not isinstance(conditions, dict) or set(conditions) != {"A", "B"}:
        raise ValueError("conditions must contain A and B")
    cells = [cell for values in conditions.values() for cell in values]
    if any(not isinstance(cell, str) for cell in cells) or len(set(cells)) != len(cells):
        raise ValueError("cell ids must be globally unique")
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or {item.get("slot") for item in candidates} != set(cells):
        raise ValueError("candidate slots must match condition cells")
    if len(candidates) != len(cells) or len({item.get("expected_model") for item in candidates}) != len(conditions["A"]):
        raise ValueError("candidate model mapping is incomplete")
    waves = manifest.get("waves")
    if not isinstance(waves, list) or [wave.get("id") for wave in waves] != ["G1-A", "G2-B", "G1-B", "G2-A"]:
        raise ValueError("waves must use the declared four-wave order")
    wave_cells = [slot for wave in waves for slot in wave.get("slots", [])]
    if len(wave_cells) != len(cells) or set(wave_cells) != set(cells):
        raise ValueError("waves must partition all cells")
    for wave in waves:
        expected_condition = wave["id"].split("-", 1)[1]
        if wave.get("condition") != expected_condition or any(slot not in conditions[expected_condition] for slot in wave.get("slots", [])):
            raise ValueError("wave condition does not match its slots")
    axes = manifest.get("ranking_axes")
    if not isinstance(axes, dict):
        raise ValueError("missing ranking axes")
    for board in ("strict", "lenient"):
        board_axes = axes.get(board)
        if not isinstance(board_axes, list) or not board_axes:
            raise ValueError(f"missing {board} ranking axes")
        if any(not isinstance(axis, dict) or axis.get("direction") not in ("high", "low") for axis in board_axes):
            raise ValueError(f"invalid {board} axis direction")
    capabilities = manifest.get("capabilities", {})
    if not isinstance(capabilities, dict) or any(not isinstance(value, list) for value in capabilities.values()):
        raise ValueError("capabilities must be lists")
    for values in capabilities.values():
        if any(not all(key in item for key in ("id", "task", "file", "probe")) for item in values):
            raise ValueError("capability entries are incomplete")
    timeout = manifest.get("isolation", {}).get("criterion_timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("criterion timeout must be positive")


def atomic_json(path, value, refuse_overwrite=True):
    """Write JSON through a same-directory temporary file without clobbering."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = pathlib.Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if refuse_overwrite:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise FileExistsError(f"refusing to overwrite {path}") from None
            except OSError:
                if path.exists():
                    raise FileExistsError(f"refusing to overwrite {path}") from None
                os.rename(temporary, path)
            else:
                temporary.unlink()
        else:
            os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _token(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _record_from_dict(node):
    input_keys = ("input_tokens", "prompt_tokens", "inputTokenCount")
    output_keys = ("output_tokens", "completion_tokens", "outputTokenCount")
    total_keys = ("total_tokens", "totalTokenCount")
    cache_keys = ("cache_tokens", "cached_tokens", "cache_read_input_tokens", "cache_read_tokens")
    wire_input_keys = ("inputOther",)
    wire_output_keys = ("output",)
    wire_cache_keys = ("inputCacheRead", "inputCacheCreation")
    all_keys = input_keys + output_keys + total_keys + cache_keys + wire_input_keys + wire_output_keys + wire_cache_keys
    if not isinstance(node, dict) or not any(key in node for key in all_keys):
        return None
    def first(keys):
        for key in keys:
            if key in node:
                return _token(node[key])
        return 0
    wire_schema = any(key in node for key in wire_input_keys + wire_output_keys + wire_cache_keys)
    if wire_schema:
        inputs = first(wire_input_keys)
        outputs = first(wire_output_keys)
        cache = sum(first((key,)) for key in wire_cache_keys)
        total = first(total_keys) if any(key in node for key in total_keys) else inputs + outputs + cache
    else:
        inputs = first(input_keys)
        outputs = first(output_keys)
        cache = first(cache_keys)
        total = first(total_keys) if any(key in node for key in total_keys) else inputs + outputs
    return {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": total, "cache_tokens": cache}


def _usage_records(value):
    if isinstance(value, dict):
        record = _record_from_dict(value)
        if record is not None:
            yield record
        for item in value.values():
            yield from _usage_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _usage_records(item)


def _dedupe_consecutive(records):
    """Collapse provider wire events that repeat the same usage payload."""
    output = []
    for record in records:
        key = tuple(sorted(record.items()))
        if not output or tuple(sorted(output[-1].items())) != key:
            output.append(record)
    return output


def aggregate_usage(records, available=True, sources=None):
    records = list(records)
    return {
        "available": bool(available and records),
        "record_count": len(records),
        "source_files": sorted(set(sources or [])),
        "input_tokens": sum(record["input_tokens"] for record in records),
        "output_tokens": sum(record["output_tokens"] for record in records),
        "total_tokens": sum(record["total_tokens"] for record in records),
        "cache_tokens": sum(record["cache_tokens"] for record in records),
    }


def extract_usage(value):
    """Aggregate every wire-usage object nested in a decoded JSON value."""
    return aggregate_usage(_dedupe_consecutive(_usage_records(value)), available=True)


def read_usage(slot_dir):
    """Read usage.json and JSONL wire events without executing candidate code."""
    slot_dir = pathlib.Path(slot_dir)
    usage_records = []
    event_records = []
    sources = []
    usage_path = slot_dir / "usage.json"
    if usage_path.is_file():
        try:
            with usage_path.open("r", encoding="utf-8") as handle:
                usage_records.extend(_usage_records(json.load(handle)))
            sources.append("usage.json")
        except (OSError, ValueError, UnicodeError):
            pass
    events_path = slot_dir / "events.jsonl"
    if events_path.is_file():
        try:
            with events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        try:
                            event_records.extend(_usage_records(json.loads(line)))
                        except ValueError:
                            continue
            sources.append("events.jsonl")
        except (OSError, UnicodeError):
            pass
    records = _dedupe_consecutive(usage_records) + _dedupe_consecutive(event_records)
    return aggregate_usage(records, available=True, sources=sources)


def candidate_for(manifest, slot):
    for candidate in manifest["candidates"]:
        if candidate["slot"] == slot:
            return candidate
    return {"slot": slot, "expected_model": slot, "group": None}


def tree_snapshot(root):
    """Return a deterministic relative-path to SHA-256 map for a run tree."""
    root = pathlib.Path(root).resolve()
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def changed_paths(before, after):
    """Return added, removed, or changed paths in stable order."""
    keys = set(before) | set(after)
    return sorted(path for path in keys if before.get(path) != after.get(path))
