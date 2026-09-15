"""Independent black-box supplemental checks for B11 ``dependency_layers``.

This module deliberately lives outside ``reviewer/`` and ``short/``.  It never
writes to a candidate workspace and only imports the explicitly supplied
candidate module in the subprocess launched by its CLI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import statistics
import sys
import time
from types import ModuleType
from typing import Callable, Dict, List, Sequence

try:
    from . import SCHEMA_VERSION
except ImportError:  # direct execution by an absolute script path
    SCHEMA_VERSION = 1

sys.dont_write_bytecode = True

CHECK_IDS = (
    "cross_hashability",
    "hash_collision",
    "unhashable_equal_dedup",
    "cycle_blocked_and_peeled",
    "stable_multi_parent_layers",
    "one_shot_generator",
    "deep_50000",
    "wide_50000",
)
PERFORMANCE_IDS = frozenset(("deep_50000", "wide_50000"))
PERFORMANCE_REPEATS = 3


def load_candidate(path: pathlib.Path) -> ModuleType:
    """Load one explicitly selected candidate solution module."""
    path = pathlib.Path(path).resolve()
    spec = importlib.util.spec_from_file_location("reviewer_candidate_dependency_layers", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load candidate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cross_hashability(module: ModuleType) -> None:
    """Check equal hashable and unhashable nodes use first-seen identity."""
    class HashableNode:
        def __init__(self, value: str) -> None:
            self.value = value

        def __hash__(self) -> int:
            return hash(self.value)

        def __eq__(self, other: object) -> bool:
            return getattr(other, "value", object()) == self.value

    class UnhashableNode:
        __hash__ = None

        def __init__(self, value: str) -> None:
            self.value = value

        def __eq__(self, other: object) -> bool:
            return getattr(other, "value", object()) == self.value

    first = HashableNode("same")
    equal_unhashable = UnhashableNode("same")
    result = module.dependency_layers([(first, "root"), (equal_unhashable, "root")])
    assert result == [["root"], [first]], result
    assert result[1][0] is first


def hash_collision(module: ModuleType) -> None:
    """Check hash collisions do not merge unequal nodes."""
    class Collision:
        def __init__(self, value: str) -> None:
            self.value = value

        def __hash__(self) -> int:
            return 1

        def __eq__(self, other: object) -> bool:
            return isinstance(other, Collision) and self.value == other.value

    left = Collision("left")
    right = Collision("right")
    result = module.dependency_layers([(left, "root"), (right, "root")])
    assert result == [["root"], [left, right]], result


def unhashable_equal_dedup(module: ModuleType) -> None:
    """Check equality de-duplication works for unhashable node values."""
    first = ["same"]
    equal = ["same"]
    result = module.dependency_layers([(first, ["root"]), (equal, ["root"])])
    assert result == [[["root"]], [first]], result
    assert result[1][0] is first


def cycle_blocked_and_peeled(module: ModuleType) -> None:
    """Check cycles report blocked descendants while peelable nodes vanish."""
    edges = [
        ("a", "b"),
        ("b", "a"),
        ("c", "a"),
        ("d", "c"),
        ("free-child", "free-root"),
    ]
    try:
        module.dependency_layers(edges)
    except module.DependencyCycleError as error:
        assert error.nodes == ("a", "b", "c", "d"), error.nodes
    else:
        raise AssertionError("cycle was not detected")


def stable_multi_parent_layers(module: ModuleType) -> None:
    """Check first-appearance order for multiple ready parents."""
    edges = [
        ("late", "root-b"),
        ("early", "root-a"),
        ("join", "late"),
        ("join", "early"),
    ]
    assert module.dependency_layers(edges) == [
        ["root-b", "root-a"],
        ["late", "early"],
        ["join"],
    ]


def one_shot_generator(module: ModuleType) -> None:
    """Check the input edge iterable is consumed exactly once."""
    class Once:
        def __init__(self) -> None:
            self.calls = 0

        def __iter__(self):
            self.calls += 1
            if self.calls != 1:
                raise AssertionError("iterated more than once")
            yield ("child", "root")

    edges = Once()
    assert module.dependency_layers(edges) == [["root"], ["child"]]
    assert edges.calls == 1


def deep_50000(module: ModuleType) -> None:
    """Check a fixed 50,000-edge deep chain without recursion."""
    result = module.dependency_layers((index, index - 1) for index in range(1, 50001))
    assert len(result) == 50001
    assert result[0] == [0] and result[-1] == [50000]


def wide_50000(module: ModuleType) -> None:
    """Check a fixed 50,000-child wide graph."""
    result = module.dependency_layers((index, 0) for index in range(1, 50001))
    assert result[0] == [0]
    assert result[1] == list(range(1, 50001))


_CHECKS: Dict[str, Callable[[ModuleType], None]] = {
    name: globals()[name] for name in CHECK_IDS
}


def _failure(check_id: str, error: BaseException, duration_ms: float = 0.0) -> dict:
    return {
        "id": check_id,
        "passed": False,
        "duration_ms": round(duration_ms, 3),
        "error_type": type(error).__name__,
        "message": str(error)[:300],
    }


def run_one(module: ModuleType, check_id: str) -> dict:
    """Run one named check and return its stable structured result."""
    if check_id not in _CHECKS:
        return _failure(check_id, KeyError("unknown supplemental check"))
    callback = _CHECKS[check_id]
    repetitions = PERFORMANCE_REPEATS if check_id in PERFORMANCE_IDS else 1
    durations: List[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        try:
            callback(module)
        except BaseException as error:
            return _failure(check_id, error, (time.perf_counter() - started) * 1000)
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "id": check_id,
        "passed": True,
        "duration_ms": round(statistics.median(durations), 3),
        "error_type": None,
        "message": None,
    }


# Public named checks return the required structured shape.  ``_CHECKS`` above
# retains the assertion callbacks, so these wrappers remain independently
# callable without changing suite execution.
def cross_hashability(module: ModuleType) -> dict:
    return run_one(module, "cross_hashability")


def hash_collision(module: ModuleType) -> dict:
    return run_one(module, "hash_collision")


def unhashable_equal_dedup(module: ModuleType) -> dict:
    return run_one(module, "unhashable_equal_dedup")


def cycle_blocked_and_peeled(module: ModuleType) -> dict:
    return run_one(module, "cycle_blocked_and_peeled")


def stable_multi_parent_layers(module: ModuleType) -> dict:
    return run_one(module, "stable_multi_parent_layers")


def one_shot_generator(module: ModuleType) -> dict:
    return run_one(module, "one_shot_generator")


def deep_50000(module: ModuleType) -> dict:
    return run_one(module, "deep_50000")


def wide_50000(module: ModuleType) -> dict:
    return run_one(module, "wide_50000")


def run_path_one(path: pathlib.Path, check_id: str) -> dict:
    """Load an explicit candidate and run exactly one isolated check."""
    path = pathlib.Path(path).resolve()
    try:
        module = load_candidate(path)
    except BaseException as error:
        return _failure(check_id, error)
    return run_one(module, check_id)


def run(path: pathlib.Path) -> dict:
    """Run all checks in-process; runner.py uses the single-check CLI instead."""
    path = pathlib.Path(path).resolve()
    results = [run_path_one(path, check_id) for check_id in CHECK_IDS]
    passed = sum(1 for item in results if item["passed"])
    performance = [
        item["duration_ms"] for item in results
        if item["id"] in PERFORMANCE_IDS and item["passed"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate": str(path),
        "passed": passed,
        "total": len(results),
        "checks": results,
        "performance_median_ms": round(statistics.median(performance), 3) if performance else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=pathlib.Path)
    parser.add_argument("--check", choices=CHECK_IDS, help="run exactly one check")
    args = parser.parse_args(argv)
    result = run_path_one(args.candidate, args.check) if args.check else run(args.candidate)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
