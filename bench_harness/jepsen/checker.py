"""Distributed invariant analysis (the "checker").

Covers ``benchmark_v3/bench_harness/jepsen/checker.py``:

* :meth:`InvariantChecker.check_single_leader` — at most one leader per
  term/epoch across the recorded election history.
* :meth:`InvariantChecker.check_commit_persistence` — every acknowledged /
  committed record still exists on the surviving nodes after partition
  healing and crashes (never lost).
* :meth:`InvariantChecker.check_final_consistency` — all surviving nodes
  converge to identical state.
* :meth:`InvariantChecker.check_linearizability` — single-key
  linearizability of client read/write operations via interval (real-time
  order) analysis; multi-key workloads are grouped per key.
* :meth:`InvariantChecker.run_all` — convenience entry point returning an
  overall verdict plus per-check :class:`CheckResult` records.

All inputs accept plain dicts *or* objects with attributes, so checkers work
directly on harness dataclasses as well as on deserialized JSON logs.

Standard library only, cross-platform (Windows + POSIX).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = ["CheckResult", "InvariantChecker"]


@dataclass
class CheckResult:
    """Outcome of one invariant check."""

    name: str
    passed: bool
    detail: str = ""
    diagnostics: str = ""

    @property
    def ok(self) -> bool:  # alias that reads well in assertions
        return self.passed


def _get(record: Any, *names: str, default: Any = None) -> Any:
    """Read the first present key/attribute among *names* from *record*."""
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
        return default
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return default


def _freeze(obj: Any) -> Any:
    """Convert *obj* into a canonical, JSON-serializable structure."""
    if isinstance(obj, Mapping):
        return {str(k): _freeze(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [_freeze(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_freeze(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "__dict__"):
        return _freeze(vars(obj))
    return str(obj)


def _canonical(obj: Any) -> str:
    return json.dumps(_freeze(obj), sort_keys=True, default=str)


def _extract_records(state: Any) -> list[Any]:
    """Pull the record collection out of a node-state container."""
    if isinstance(state, Mapping):
        for key in ("records", "data", "store", "log", "entries"):
            if key in state:
                inner = state[key]
                if isinstance(inner, Mapping):
                    return list(inner.values())
                return list(inner)
        return [state]
    for attr in ("records", "data", "store", "log", "entries"):
        if hasattr(state, attr):
            inner = getattr(state, attr)
            if isinstance(inner, Mapping):
                return list(inner.values())
            return list(inner)
    if isinstance(state, (list, tuple, set, frozenset)):
        return list(state)
    return [state]


class InvariantChecker:
    """Static collection of distributed-system invariant checks."""

    # -- single leader ------------------------------------------------------
    @staticmethod
    def check_single_leader(history: Iterable[Any]) -> CheckResult:
        """Assert at most one distinct leader per term/epoch.

        *history* entries provide a term (``term`` / ``epoch``) and a leader
        (``leader`` / ``leader_id``). Entries without a leader are skipped
        (they carry no election claim).
        """
        leaders: dict[Any, set[str]] = {}
        for entry in history or []:
            term = _get(entry, "term", "epoch")
            leader = _get(entry, "leader", "leader_id")
            if term is None or leader is None:
                continue
            leaders.setdefault(term, set()).add(str(leader))
        violations = {t: sorted(v) for t, v in leaders.items() if len(v) > 1}
        if violations:
            sample = list(violations.items())[:5]
            return CheckResult(
                name="single_leader",
                passed=False,
                detail=f"{len(violations)} term(s) elected >1 leader",
                diagnostics=json.dumps(sample, default=str),
            )
        return CheckResult(
            name="single_leader",
            passed=True,
            detail=f"{len(leaders)} term(s) checked, at most one leader each",
        )

    # -- commit persistence ---------------------------------------------------
    @staticmethod
    def check_commit_persistence(
        committed_records: Iterable[Any],
        node_states: Mapping[Any, Any],
        survivors: Iterable[Any] | None = None,
        require_all: bool = False,
    ) -> CheckResult:
        """Assert no acknowledged/committed record was lost.

        Every committed record must be found on the surviving nodes — on at
        least one survivor by default (``require_all=False``: "never lost"),
        or on *every* survivor with ``require_all=True`` (strict quorum
        durability). Pass ``survivors`` to exclude dead nodes; when omitted,
        every node in *node_states* counts as surviving.
        """
        committed = list(committed_records or [])
        if survivors is None:
            survivor_ids = list((node_states or {}).keys())
        else:
            survivor_ids = list(survivors)
        if not committed:
            return CheckResult(
                name="commit_persistence", passed=True, detail="no committed records"
            )
        if not survivor_ids:
            return CheckResult(
                name="commit_persistence",
                passed=False,
                detail="no surviving nodes to verify against",
            )
        per_node: dict[Any, set[str]] = {}
        for nid in survivor_ids:
            if nid not in (node_states or {}):
                per_node[nid] = set()
                continue
            try:
                per_node[nid] = {_canonical(r) for r in _extract_records(node_states[nid])}
            except TypeError:
                per_node[nid] = set()
        union = set().union(*per_node.values()) if per_node else set()
        if require_all:
            inter = (
                set.intersection(*per_node.values()) if per_node else set()
            )
            present = inter
        else:
            present = union
        missing = [r for r in committed if _canonical(r) not in present]
        if missing:
            return CheckResult(
                name="commit_persistence",
                passed=False,
                detail=f"{len(missing)}/{len(committed)} committed record(s) lost",
                diagnostics=json.dumps(_freeze(missing[:5]), default=str),
            )
        scope = "every survivor" if require_all else ">=1 survivor"
        return CheckResult(
            name="commit_persistence",
            passed=True,
            detail=f"{len(committed)} committed record(s) present on {scope}",
        )

    # -- final consistency ------------------------------------------------------
    @staticmethod
    def check_final_consistency(
        node_states: Mapping[Any, Any],
        survivors: Iterable[Any] | None = None,
    ) -> CheckResult:
        """Assert all surviving nodes converged to identical state."""
        states = node_states or {}
        ids = list(survivors) if survivors is not None else list(states.keys())
        if not ids:
            return CheckResult(
                name="final_consistency",
                passed=True,
                detail="no surviving nodes; nothing to diverge",
            )
        digests: dict[str, list[Any]] = {}
        for nid in ids:
            if nid not in states:
                digests.setdefault("<missing>", []).append(nid)
                continue
            try:
                digest = _canonical(_extract_records(states[nid]))
            except TypeError as exc:
                return CheckResult(
                    name="final_consistency",
                    passed=False,
                    detail=f"node {nid!r} state is not comparable: {exc}",
                )
            digests.setdefault(digest, []).append(nid)
        if len(digests) == 1:
            return CheckResult(
                name="final_consistency",
                passed=True,
                detail=f"{len(ids)} survivor(s) converged to identical state",
            )
        summary = {f"group_{i}": sorted(map(str, members)) for i, members in enumerate(digests.values())}
        return CheckResult(
            name="final_consistency",
            passed=False,
            detail=f"{len(digests)} divergent state group(s) across {len(ids)} survivor(s)",
            diagnostics=json.dumps(summary),
        )

    # -- linearizability ----------------------------------------------------------
    @staticmethod
    def check_linearizability(
        operations: Iterable[Any],
        initial_value: Any = None,
    ) -> CheckResult:
        """Validate single-key linearizability of client operations.

        Write op fields: ``type="write"``, ``key`` (default key when absent),
        ``value``, ``start``/``start_time``, ``end``/``end_time`` (absent end
        = still in flight / concurrent). Read op fields: ``type="read"``,
        ``key``, ``value`` (the returned value), ``start``, ``end``.

        A read is linearizable when it returns either the initial value with
        no write having completed before the read began, or the value of a
        write overlapping-or-preceding the read with no *newer*-valued write
        having fully completed before the read started.
        """
        ops = list(operations or [])
        if not ops:
            return CheckResult(
                name="linearizability", passed=True, detail="no operations"
            )
        by_key: dict[Any, list[Any]] = {}
        for op in ops:
            by_key.setdefault(_get(op, "key", default="__default__"), []).append(op)
        for key, group in by_key.items():
            writes = [
                w for w in group if str(_get(w, "type", "op", default="")).lower().startswith("w")
            ]
            reads = [
                r for r in group if str(_get(r, "type", "op", default="")).lower().startswith("r")
            ]
            parsed_w = [
                (
                    _get(w, "value"),
                    _as_float(_get(w, "start", "start_time")),
                    _as_float(_get(w, "end", "end_time", default=None)),
                )
                for w in writes
            ]
            for read in reads:
                value = _get(read, "value", "result")
                start = _as_float(_get(read, "start", "start_time"))
                end = _as_float(_get(read, "end", "end_time", default=start))
                if start is None or end is None:
                    return CheckResult(
                        name="linearizability",
                        passed=False,
                        detail=f"read on key {key!r} misses interval bounds",
                        diagnostics=json.dumps(_freeze(read), default=str),
                    )
                ok, reason = _check_read_linearizable(
                    value, start, end, parsed_w, initial_value
                )
                if not ok:
                    return CheckResult(
                        name="linearizability",
                        passed=False,
                        detail=f"key {key!r}: {reason}",
                        diagnostics=json.dumps(_freeze(read), default=str),
                    )
        return CheckResult(
            name="linearizability",
            passed=True,
            detail=f"{len(ops)} operation(s) across {len(by_key)} key(s) linearizable",
        )

    # -- bundle ---------------------------------------------------------------
    @staticmethod
    def run_all(
        history: Iterable[Any] | None = None,
        committed_records: Iterable[Any] | None = None,
        node_states: Mapping[Any, Any] | None = None,
        survivors: Iterable[Any] | None = None,
        operations: Iterable[Any] | None = None,
        initial_value: Any = None,
        require_all_commits: bool = False,
    ) -> tuple[bool, list[CheckResult]]:
        """Run every applicable check; return ``(overall_passed, results)``.

        Checks with no input provided are skipped (not failed).
        """
        results: list[CheckResult] = []
        if history is not None:
            results.append(InvariantChecker.check_single_leader(history))
        if committed_records is not None:
            results.append(
                InvariantChecker.check_commit_persistence(
                    committed_records,
                    node_states or {},
                    survivors=survivors,
                    require_all=require_all_commits,
                )
            )
        if node_states is not None:
            results.append(
                InvariantChecker.check_final_consistency(node_states, survivors)
            )
        if operations is not None:
            results.append(
                InvariantChecker.check_linearizability(operations, initial_value)
            )
        return all(r.passed for r in results), results


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_read_linearizable(
    value: Any,
    start: float,
    end: float,
    writes: list[tuple[Any, float | None, float | None]],
    initial_value: Any,
) -> tuple[bool, str]:
    """Core stale/phantom-read test for one read against parsed writes."""
    timed = [(v, s, e) for (v, s, e) in writes if s is not None]
    candidates = [w for w in timed if w[0] == value and w[1] <= end]
    if not candidates:
        if value == initial_value:
            earlier = [w for w in timed if w[2] is not None and w[2] <= start]
            if earlier:
                return False, "stale initial-value read after completed write(s)"
            return True, ""
        return False, f"phantom read of value {value!r} (no such write)"
    newer = [
        w for w in timed if w[0] != value and w[2] is not None and w[2] <= start
    ]
    if newer:
        latest = max(w[2] for w in newer if w[2] is not None)
        fresh = [w for w in candidates if w[2] is None or w[2] >= latest]
        if not fresh:
            return False, f"stale read of {value!r} (newer write completed first)"
    return True, ""


# ---------------------------------------------------------------------------
# Unit self-tests (stdlib only; safe on Windows + POSIX, no network).
# ---------------------------------------------------------------------------


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    counts = [0, 0]

    def check(name: str, cond: bool, extra: str = "") -> None:
        counts[0 if cond else 1] += 1
        suffix = f" [{extra}]" if extra and not cond else ""
        print(f"{'PASS' if cond else 'FAIL'} checker::{name}{suffix}", flush=True)

    # --- single leader ---
    ok_hist = [
        {"term": 1, "leader": "n1"},
        {"term": 1, "leader": "n1"},
        {"term": 2, "leader": "n2"},
    ]
    check("leader_ok", InvariantChecker.check_single_leader(ok_hist).passed)
    bad_hist = [
        {"term": 1, "leader": "n1"},
        {"term": 1, "leader": "n2"},
    ]
    r = InvariantChecker.check_single_leader(bad_hist)
    check("leader_split_brain", not r.passed and r.name == "single_leader")
    check("leader_empty", InvariantChecker.check_single_leader([]).passed)

    class ObjEntry:
        def __init__(self, epoch, leader_id):
            self.epoch = epoch
            self.leader_id = leader_id

    check(
        "leader_objects",
        InvariantChecker.check_single_leader(
            [ObjEntry(3, "n1"), ObjEntry(3, "n1")]
        ).passed,
    )

    # --- commit persistence ---
    committed = [{"id": 1}, {"id": 2}, {"id": 3}]
    states = {
        "n1": [{"id": 1}, {"id": 2}, {"id": 3}],
        "n2": {"records": [{"id": 1}, {"id": 2}, {"id": 3}]},
        "dead": [{"id": 1}],
    }
    check(
        "commits_survive",
        InvariantChecker.check_commit_persistence(
            committed, states, survivors=["n1", "n2"]
        ).passed,
    )
    lost_states = {"n1": [{"id": 1}], "n2": [{"id": 2}]}
    r = InvariantChecker.check_commit_persistence(committed, lost_states)
    check("commits_lost", not r.passed)
    partial = {"n1": [{"id": 1}, {"id": 2}, {"id": 3}], "n2": [{"id": 1}]}
    check(
        "commits_union_ok",
        InvariantChecker.check_commit_persistence(committed, partial).passed,
    )
    r = InvariantChecker.check_commit_persistence(
        committed, partial, require_all=True
    )
    check("commits_strict_fails", not r.passed)
    check(
        "commits_empty",
        InvariantChecker.check_commit_persistence([], lost_states).passed,
    )

    # --- final consistency ---
    check(
        "consistent",
        InvariantChecker.check_final_consistency(
            {"n1": {"a": 1}, "n2": {"a": 1}}
        ).passed,
    )
    check(
        "divergent",
        not InvariantChecker.check_final_consistency(
            {"n1": {"a": 1}, "n2": {"a": 2}}
        ).passed,
    )
    check(
        "ignores_dead",
        InvariantChecker.check_final_consistency(
            {"n1": {"a": 1}, "dead": {"a": 9}}, survivors=["n1"]
        ).passed,
    )

    # --- linearizability ---
    linear_ops = [
        {"type": "write", "key": "x", "value": 1, "start": 0.0, "end": 1.0},
        {"type": "read", "key": "x", "value": 1, "start": 1.5, "end": 2.0},
        {"type": "write", "key": "x", "value": 2, "start": 3.0, "end": 4.0},
        {"type": "read", "key": "x", "value": 2, "start": 4.5, "end": 5.0},
    ]
    check(
        "linear_ok",
        InvariantChecker.check_linearizability(linear_ops).passed,
    )
    stale_ops = [
        {"type": "write", "key": "x", "value": 1, "start": 0.0, "end": 1.0},
        {"type": "write", "key": "x", "value": 2, "start": 2.0, "end": 3.0},
        {"type": "read", "key": "x", "value": 1, "start": 3.5, "end": 4.0},
    ]
    check(
        "linear_stale",
        not InvariantChecker.check_linearizability(stale_ops).passed,
    )
    phantom_ops = [{"type": "read", "key": "x", "value": 99, "start": 0.0, "end": 1.0}]
    check(
        "linear_phantom",
        not InvariantChecker.check_linearizability(phantom_ops).passed,
    )
    # Concurrent write may legitimately be observed.
    concurrent_ops = [
        {"type": "write", "key": "x", "value": 1, "start": 0.0, "end": 10.0},
        {"type": "read", "key": "x", "value": 1, "start": 1.0, "end": 2.0},
    ]
    check(
        "linear_concurrent",
        InvariantChecker.check_linearizability(concurrent_ops).passed,
    )
    # Initial-value read with no writes at all.
    check(
        "linear_initial",
        InvariantChecker.check_linearizability(
            [{"type": "read", "key": "x", "value": None, "start": 0.0, "end": 1.0}]
        ).passed,
    )
    # Multi-key isolation: stale on x must not poison y.
    multi_ops = stale_ops + [
        {"type": "write", "key": "y", "value": "a", "start": 0.0, "end": 1.0},
        {"type": "read", "key": "y", "value": "a", "start": 2.0, "end": 2.5},
    ]
    check(
        "linear_multikey",
        not InvariantChecker.check_linearizability(multi_ops).passed,
    )

    # --- run_all bundle ---
    passed, results = InvariantChecker.run_all(
        history=ok_hist,
        committed_records=committed,
        node_states={"n1": committed, "n2": committed},
        operations=linear_ops,
    )
    check("run_all_pass", passed and len(results) == 4)
    passed2, _ = InvariantChecker.run_all(
        history=bad_hist, operations=linear_ops
    )
    check("run_all_fail", not passed2)
    passed3, results3 = InvariantChecker.run_all()
    check("run_all_empty", passed3 and results3 == [])

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"checker self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
