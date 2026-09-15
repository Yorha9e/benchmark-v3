"""Supplemental black-box checks for the B11 dependency_layers debug study."""

import importlib.util
import json
import pathlib
import sys
import time


def load_candidate(path):
    spec = importlib.util.spec_from_file_location("supplemental_candidate", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load candidate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(path):
    module = load_candidate(path)
    results = {}

    def check(name, callback):
        try:
            callback()
        except BaseException as error:
            results[name] = {
                "passed": False,
                "error_type": type(error).__name__,
                "message": str(error)[:300],
            }
        else:
            results[name] = {"passed": True}

    def cross_hashability():
        class HashableNode:
            def __init__(self, value):
                self.value = value

            def __hash__(self):
                return hash(self.value)

            def __eq__(self, other):
                return getattr(other, "value", object()) == self.value

        class UnhashableNode:
            __hash__ = None

            def __init__(self, value):
                self.value = value

            def __eq__(self, other):
                return getattr(other, "value", object()) == self.value

        first = HashableNode("same")
        equal_unhashable = UnhashableNode("same")
        result = module.dependency_layers(
            [(first, "root"), (equal_unhashable, "root")]
        )
        assert result == [["root"], [first]], result
        assert result[1][0] is first

    def hash_collision():
        class Collision:
            def __init__(self, value):
                self.value = value

            def __hash__(self):
                return 1

            def __eq__(self, other):
                return isinstance(other, Collision) and self.value == other.value

        left = Collision("left")
        right = Collision("right")
        result = module.dependency_layers([(left, "root"), (right, "root")])
        assert result == [["root"], [left, right]], result

    def unhashable_equal_dedup():
        first = ["same"]
        equal = ["same"]
        result = module.dependency_layers([(first, ["root"]), (equal, ["root"])])
        assert result == [[['root']], [first]], result
        assert result[1][0] is first

    def cycle_blocked_and_peeled():
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

    def stable_multi_parent_layers():
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

    def one_shot_generator():
        class Once:
            def __init__(self):
                self.calls = 0

            def __iter__(self):
                self.calls += 1
                if self.calls != 1:
                    raise AssertionError("iterated more than once")
                yield ("child", "root")

        edges = Once()
        assert module.dependency_layers(edges) == [["root"], ["child"]]
        assert edges.calls == 1

    timings = {}

    def deep_50000():
        started = time.perf_counter()
        result = module.dependency_layers((index, index - 1) for index in range(1, 50001))
        timings["deep_50000_seconds"] = time.perf_counter() - started
        assert len(result) == 50001
        assert result[0] == [0] and result[-1] == [50000]

    def wide_50000():
        started = time.perf_counter()
        result = module.dependency_layers((index, 0) for index in range(1, 50001))
        timings["wide_50000_seconds"] = time.perf_counter() - started
        assert result[0] == [0]
        assert result[1] == list(range(1, 50001))

    check("cross_hashability", cross_hashability)
    check("hash_collision", hash_collision)
    check("unhashable_equal_dedup", unhashable_equal_dedup)
    check("cycle_blocked_and_peeled", cycle_blocked_and_peeled)
    check("stable_multi_parent_layers", stable_multi_parent_layers)
    check("one_shot_generator", one_shot_generator)
    check("deep_50000", deep_50000)
    check("wide_50000", wide_50000)

    return {
        "schema_version": 1,
        "candidate": str(path),
        "passed": sum(item["passed"] for item in results.values()),
        "total": len(results),
        "checks": results,
        "timings": timings,
    }


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: supplemental_debug_checks.py CANDIDATE_FILE")
    path = pathlib.Path(sys.argv[1]).resolve()
    print(json.dumps(run(path), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
