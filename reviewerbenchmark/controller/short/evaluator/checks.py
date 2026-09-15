"""Hidden criterion checks. Each function must be process-independent."""

import gc
import math
import weakref


def _expect_raises(exception, callback):
    try:
        callback()
    except exception as caught:
        return caught
    if isinstance(exception, tuple):
        name = " or ".join(item.__name__ for item in exception)
    else:
        name = exception.__name__
    raise AssertionError(f"expected {name}")


def rp_delete_identity(module):
    class EqualSentinel:
        def __eq__(self, other): return isinstance(other, EqualSentinel)
    sentinel = EqualSentinel()
    equal_but_distinct = EqualSentinel()
    result = module.apply_patch({"drop": 1, "keep": 2}, {"drop": sentinel, "keep": equal_but_distinct}, delete=sentinel)
    assert result == {"keep": equal_but_distinct}
    assert result["keep"] is not sentinel
    custom = object()
    replacement_delete = object()
    result = module.apply_patch({"x": 1}, {"x": custom}, delete=replacement_delete)
    assert "x" in result
    assert result["x"] is not replacement_delete


def rp_plain_dict(module):
    class Mapping(dict):
        pass
    _expect_raises(Exception, lambda: module.apply_patch(Mapping(), {}))
    _expect_raises(Exception, lambda: module.apply_patch({}, Mapping()))
    old = Mapping(a=1)
    patch = {"x": Mapping(b=2)}
    result = module.apply_patch({"x": old}, patch)
    assert type(result["x"]) is Mapping and result["x"] == {"b": 2}
    assert "a" not in result["x"]


def rp_isolation(module):
    base = {"a": {"items": [1, {"z": 2}]}, "untouched": [3]}
    patch = {"a": {"items": [4]}, "new": {"v": [5]}}
    base_before = repr(base)
    patch_before = repr(patch)
    result = module.apply_patch(base, patch)
    assert repr(base) == base_before and repr(patch) == patch_before
    result["a"]["items"].append(9)
    result["untouched"].append(8)
    result["new"]["v"].append(7)
    assert base == {"a": {"items": [1, {"z": 2}]}, "untouched": [3]}
    assert patch == {"a": {"items": [4]}, "new": {"v": [5]}}


def rp_order_boundary(module):
    base = {"first": 1, "nest": {"a": 1, "b": 2}, "last": 3}
    patch = {"nest": {"a": module.DELETE, "c": 4}, "first": {"z": 1, "y": 2}, "new": 5}
    result = module.apply_patch(base, patch)
    assert list(result) == ["first", "nest", "last", "new"]
    assert list(result["nest"]) == ["b", "c"]
    assert list(result["first"]) == ["z", "y"]


def dl_one_shot(module):
    class Once:
        def __init__(self):
            self.calls = 0
        def __iter__(self):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("iterated twice")
            yield ("b", "a")
            yield ("c", "b")
    edges = Once()
    assert module.dependency_layers(edges) == [["a"], ["b"], ["c"]]
    assert edges.calls == 1


def dl_dependency_nodes(module):
    result = module.dependency_layers(iter([("app", "lib"), ("app", "lib"), ("test", "lib")]))
    assert result == [["lib"], ["app", "test"]]
    assert module.dependency_layers([]) == []


def dl_stable_order_cycle(module):
    assert issubclass(module.DependencyCycleError, ValueError)
    edges = [("z", "x"), ("y", "x"), ("x", "root"), ("free", "root")]
    assert module.dependency_layers(edges) == [["root"], ["x", "free"], ["z", "y"]]
    error = _expect_raises(module.DependencyCycleError, lambda: module.dependency_layers([("a", "b"), ("b", "a"), ("c", "a")]))
    assert error.nodes == ("a", "b", "c")
    _expect_raises(module.DependencyCycleError, lambda: module.dependency_layers([("self", "self")]))


def dl_deep_iterative(module):
    depth = 12000
    result = module.dependency_layers((i, i - 1) for i in range(1, depth + 1))
    assert len(result) == depth + 1
    assert result[0] == [0] and result[-1] == [depth]


def ttl_strict_types(module):
    for value in (True, 0, -1, 1.5):
        _expect_raises((TypeError, ValueError), lambda value=value: module.BoundedTTLSet(value, 1, lambda: 0))
    for value in (True, -1, math.inf, math.nan, "1"):
        _expect_raises((TypeError, ValueError), lambda value=value: module.BoundedTTLSet(1, value, lambda: 0))
    _expect_raises((TypeError, ValueError), lambda: module.BoundedTTLSet(1, 1, None))
    now = [0]
    values = module.BoundedTTLSet(1, 1, lambda: now[0])
    now[0] = True
    _expect_raises((TypeError, ValueError), lambda: values.add("x"))


def ttl_exact_expiry(module):
    now = [10.0]
    values = module.BoundedTTLSet(3, 2.0, lambda: now[0])
    values.add("x")
    now[0] = 11.999
    assert "x" in values and len(values) == 1
    now[0] = 12.0
    assert "x" not in values and len(values) == 0
    zero = module.BoundedTTLSet(1, 0, lambda: 5)
    zero.add("z")
    assert "z" not in zero


def ttl_capacity(module):
    now = [0]
    values = module.BoundedTTLSet(2, 100, lambda: now[0])
    values.add("a"); values.add("b"); values.add("c")
    assert "a" not in values and "b" in values and "c" in values
    values.add("b"); values.add("d")
    assert "c" not in values and "b" in values and "d" in values


def ttl_gc_release(module):
    class Key:
        def __init__(self, identity): self.identity = identity
        def __hash__(self): return hash(self.identity)
        def __eq__(self, other): return isinstance(other, Key) and self.identity == other.identity
    now = [0]
    values = module.BoundedTTLSet(3, 1, lambda: now[0])
    old = Key(1); old_ref = weakref.ref(old); values.add(old)
    replacement = Key(1); values.add(replacement); del old; gc.collect()
    assert old_ref() is None
    deleted = Key(2); deleted_ref = weakref.ref(deleted); values.add(deleted); values.discard(deleted); del deleted; gc.collect()
    assert deleted_ref() is None
    expired = Key(3); expired_ref = weakref.ref(expired); values.add(expired); del expired
    now[0] = 1; len(values); gc.collect()
    assert expired_ref() is None


def du_strict_syntax(module):
    assert issubclass(module.DurationParseError, ValueError)
    for value in (None, 1, b"1s"):
        error = _expect_raises(module.DurationParseError, lambda value=value: module.parse_duration(value))
        assert error.code == "type"
    for text in ("", " 1s", "1s ", "+1s", "1.5s", "1x", "s", "1 s"):
        _expect_raises(module.DurationParseError, lambda text=text: module.parse_duration(text))
    _expect_raises(module.DurationParseError, lambda: module.parse_duration("01s"))


def du_units_ranges(module):
    parsed = module.parse_duration("2d23h59m58s999ms")
    assert type(parsed) is dict
    assert list(parsed) == ["days", "hours", "minutes", "seconds", "milliseconds"]
    assert parsed == {"days": 2, "hours": 23, "minutes": 59, "seconds": 58, "milliseconds": 999}
    for text in ("24h", "60m", "60s", "1000ms", "1s2m", "1m2m", "1ms2s"):
        _expect_raises(module.DurationParseError, lambda text=text: module.parse_duration(text))


def du_normalize(module):
    cases = {"0d0h0m0s0ms": "0s", "90s": "1m30s", "1000ms": "1s", "25h": "1d1h", "1d120m3000ms": "1d2h3s"}
    for source, expected in cases.items():
        normalized = module.normalize_duration(source)
        assert normalized == expected
        assert module.normalize_duration(normalized) == normalized


def du_structured_errors(module):
    cases = [
        ("", "empty", {0}),
        ("01s", "leading_zero", {0, 1}),
        ("1s2m", "order", {2, 3}),
        ("60s", "range", {0, 1, 2}),
        ("1q", "syntax", {1}),
    ]
    for text, code, allowed_positions in cases:
        first = _expect_raises(module.DurationParseError, lambda text=text: module.parse_duration(text))
        second = _expect_raises(module.DurationParseError, lambda text=text: module.parse_duration(text))
        assert first.code == code and second.code == code
        assert type(first.position) is int and first.position in allowed_positions
        assert second.position == first.position


def ext_recursive_patch_nested_tuple(module):
    patch = {"value": ("tag", [{"x": 1}])}
    result = module.apply_patch({}, patch)
    result["value"][1][0]["x"] = 2
    assert patch["value"][1][0]["x"] == 1


def ext_duration_large_carry(module):
    assert module.normalize_duration("1000000ms") == "16m40s"


def res_dependency_20000(module):
    depth = 20000
    result = module.dependency_layers((index, index - 1) for index in range(1, depth + 1))
    assert len(result) == depth + 1
    assert result[0] == [0] and result[-1] == [depth]


def res_ttl_expiry_sweep(module):
    now = [0]
    values = module.BoundedTTLSet(1000, 1, lambda: now[0])
    for index in range(1000):
        values.add(index)
    now[0] = 1
    assert len(values) == 0
