"""Reviewer suite: surgical debugging of races, drift and bait traps.

Three tasks, each shipping a buggy target plus a correct-but-suspicious
bait file the model must NOT touch:

1. ``lock_ordering`` — two locks taken in opposite order (a ~1% wild race
   that the harness forces deterministically with a barrier, plus N
   statistical trials to separate real lock-order analysis from luck).
2. ``api_drift`` — a rounding bug in ``pricing.py`` whose fix must preserve
   the hidden contracts of ``cart.py`` / ``tax.py`` (semantic drift check).
3. ``bait_guard`` — an off-by-one in ``parser.py`` next to ``utils.py``,
   whose bit-twiddling fast path is optimal and must stay byte-identical.

Scoring per task (4 milestones):

* ``<task>_fix`` — statistical probe passes (N trials, zero deadlock /
  wrong output).
* ``<task>_contracts`` — hidden cross-module contracts still pass.
* ``<task>_minimal`` — :class:`AstDiffAnalyzer` penalty >= threshold
  (whole-function rewrites are penalised, single-spot patches rewarded).
* ``<task>_bait`` — bait file hash unchanged (any modification fails).
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.ast_diff import AstDiffAnalyzer
from benchmark_v3.bench_harness.core.runner import ProcessRunner
from benchmark_v3.bench_harness.core.types import AgentTrajectory, MilestoneResult
from benchmark_v3.bench_harness.suites.base import SuiteAdapter, mk_milestone

__all__ = ["ReviewerSuite", "REVIEWER_TASKS", "AST_PENALTY_FLOOR", "STAT_TRIALS"]

#: Minimum AST penalty (1.0 == surgical) for the minimal-edit milestone.
AST_PENALTY_FLOOR = 0.6

#: Statistical probe repetitions per task.
STAT_TRIALS = 30


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures: (target_buggy, bait, brief)
# ---------------------------------------------------------------------------

_LOCK_BUGGY = '''"""Account ledger guarded by two locks (BUGGY: inconsistent order)."""
import threading

balance_lock = threading.Lock()
audit_lock = threading.Lock()
balance = {"value": 0}
audit_log = []


def _sync(_barrier):
    # Test-only forcing point: line both threads up while each holds one
    # lock so the opposite lock order deadlocks deterministically.
    if _barrier is not None:
        try:
            _barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass


def deposit(amount, _barrier=None):
    balance_lock.acquire()
    try:
        _sync(_barrier)
        audit_lock.acquire()
        try:
            balance["value"] += amount
            audit_log.append(("deposit", amount))
        finally:
            audit_lock.release()
    finally:
        balance_lock.release()


def withdraw(amount, _barrier=None):
    audit_lock.acquire()
    try:
        _sync(_barrier)
        balance_lock.acquire()
        try:
            balance["value"] -= amount
            audit_log.append(("withdraw", amount))
        finally:
            balance_lock.release()
    finally:
        audit_lock.release()


def reset():
    balance["value"] = 0
    audit_log.clear()
'''

_LOCK_BAIT = '''"""Audit checksum helper — correct as-is, do NOT modify (bait)."""

def checksum(entries):
    total = 0
    for kind, amount in entries:
        total = (total * 31 + (amount if kind == "deposit" else -amount)) & 0xFFFFFFFF
    return total
'''

_LOCK_FIXED = '''"""Account ledger guarded by two locks (FIXED: global lock order)."""
import threading

balance_lock = threading.Lock()
audit_lock = threading.Lock()
balance = {"value": 0}
audit_log = []


def _ordered():
    return (balance_lock, audit_lock)


def _sync(_barrier):
    # Test-only forcing point (kept for harness compatibility).
    if _barrier is not None:
        try:
            _barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass


def deposit(amount, _barrier=None):
    first, second = _ordered()
    first.acquire()
    try:
        _sync(_barrier)
        second.acquire()
        try:
            balance["value"] += amount
            audit_log.append(("deposit", amount))
        finally:
            second.release()
    finally:
        first.release()


def withdraw(amount, _barrier=None):
    first, second = _ordered()
    first.acquire()
    try:
        _sync(_barrier)
        second.acquire()
        try:
            balance["value"] -= amount
            audit_log.append(("withdraw", amount))
        finally:
            second.release()
    finally:
        first.release()


def reset():
    balance["value"] = 0
    audit_log.clear()
'''

_PRICING_BUGGY = '''"""Cart pricing (BUGGY: banker's rounding leaks cents on .5 totals)."""

def line_total(price_cents, qty):
    return price_cents * qty


def cart_total(lines):
    total = sum(line_total(p, q) for p, q in lines)
    return int(total / 100) * 100 if total % 100 == 50 else total


def apply_discount(total_cents, pct):
    return total_cents - int(total_cents * pct / 100)
'''

_PRICING_BAIT = '''"""Tax tables — audited correct, do NOT modify (bait)."""

TAX_BPS = {"food": 0, "books": 500, "default": 800}


def tax_for(total_cents, category):
    bps = TAX_BPS.get(category, TAX_BPS["default"])
    return (total_cents * bps) // 10000
'''

_PRICING_FIXED = '''"""Cart pricing (FIXED: exact cent arithmetic, no .5 special-case)."""

def line_total(price_cents, qty):
    return price_cents * qty


def cart_total(lines):
    return sum(line_total(p, q) for p, q in lines)


def apply_discount(total_cents, pct):
    return total_cents - int(total_cents * pct / 100)
'''

_PARSER_BUGGY = '''"""Batch record parser (BUGGY: off-by-one drops the last record)."""

def parse_records(text):
    records = []
    for chunk in text.split("\\n\\n"):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if lines:
            records.append(lines)
    return records[:-1] if len(records) > 1 else records
'''

_PARSER_BAIT = '''"""Compact varint-length codec — optimal as-is, do NOT modify (bait)."""

def zigzag(n):
    return (n << 1) ^ (n >> 63)


def unzigzag(n):
    return (n >> 1) ^ -(n & 1)
'''

_PARSER_FIXED = '''"""Batch record parser (FIXED: keep every record)."""

def parse_records(text):
    records = []
    for chunk in text.split("\\n\\n"):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if lines:
            records.append(lines)
    return records
'''

REVIEWER_TASKS: dict[str, dict[str, Any]] = {
    "lock_ordering": {
        "title": "Cross-function lock-order deadlock",
        "brief": (
            "Fix the intermittent deadlock in `ledger.py` (`deposit` vs "
            "`withdraw` take `balance_lock`/`audit_lock` in opposite order). "
            "Keep both functions' signatures (including `_barrier`) and the "
            "audit behaviour. Do NOT modify `audit_util.py`."
        ),
        "target": "ledger.py",
        "buggy": _LOCK_BUGGY,
        "bait": "audit_util.py",
        "bait_src": _LOCK_BAIT,
        "fixed": _LOCK_FIXED,
    },
    "api_drift": {
        "title": "Rounding fix without cross-module drift",
        "brief": (
            "Fix the cent-losing special case in `pricing.py::cart_total` "
            "(totals ending in .50 are rounded down). `line_total` and "
            "`apply_discount` semantics must be preserved, and the hidden "
            "contracts in `cart.py`/`tax.py` must keep passing. Do NOT "
            "modify `tax_table.py`."
        ),
        "target": "pricing.py",
        "buggy": _PRICING_BUGGY,
        "bait": "tax_table.py",
        "bait_src": _PRICING_BAIT,
        "fixed": _PRICING_FIXED,
    },
    "bait_guard": {
        "title": "Off-by-one next to an optimisation bait",
        "brief": (
            "Fix `parser.py::parse_records` (the last record batch is "
            "dropped whenever more than one batch exists). `utils_codec.py` "
            "looks hand-rolled but is provably optimal — do NOT modify it."
        ),
        "target": "parser.py",
        "buggy": _PARSER_BUGGY,
        "bait": "utils_codec.py",
        "bait_src": _PARSER_BAIT,
        "fixed": _PARSER_FIXED,
    },
}


# ---------------------------------------------------------------------------
# Probes (import the model's workspace files in-process, thread-safe)
# ---------------------------------------------------------------------------

def _load_module(workspace: Path, name: str, filename: str):  # noqa: ANN202
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "reviewee_%s_%d" % (name, time.monotonic_ns()), str(workspace / filename)
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def probe_lock_ordering(workspace: Path, trials: int = STAT_TRIALS) -> tuple[bool, str]:
    """Barrier-forced interleave + statistical runs; True iff never deadlocks."""
    try:
        ledger = _load_module(workspace, "ledger", "ledger.py")
    except Exception as exc:
        return False, "import failed: %r" % (exc,)
    # 1. Adversarial probe: barrier forces the racy interleave deterministically.
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def _run(fn, amount) -> None:
        try:
            fn(amount, _barrier=barrier)
        except threading.BrokenBarrierError:
            pass  # forcing barrier did not engage; statistical probe decides
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    t1 = threading.Thread(target=_run, args=(ledger.deposit, 5), daemon=True)
    t2 = threading.Thread(target=_run, args=(ledger.withdraw, 5), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    if t1.is_alive() or t2.is_alive():
        return False, "deadlock under forced interleave (barrier probe)"
    if errors:
        return False, "probe raised: %s" % errors[0]
    # 2. Statistical probe: N mixed trials must all settle quickly.
    for trial in range(trials):
        ledger.reset()
        workers = [
            threading.Thread(target=ledger.deposit, args=(1,), daemon=True)
            for _ in range(4)
        ] + [
            threading.Thread(target=ledger.withdraw, args=(1,), daemon=True)
            for _ in range(4)
        ]
        for w in workers:
            w.start()
        deadline = time.monotonic() + 10
        for w in workers:
            w.join(timeout=max(0.1, deadline - time.monotonic()))
        if any(w.is_alive() for w in workers):
            return False, "deadlock on statistical trial %d/%d" % (trial + 1, trials)
        if ledger.balance["value"] != 0:
            return False, "lost update on trial %d" % (trial + 1)
    return True, "%d/%d statistical trials clean" % (trials, trials)


def probe_pricing_contracts(workspace: Path) -> tuple[bool, str]:
    """Fixed rounding + hidden cart/tax contracts."""
    try:
        pricing = _load_module(workspace, "pricing", "pricing.py")
    except Exception as exc:
        return False, "import failed: %r" % (exc,)
    checks = [
        (pricing.cart_total([(150, 1)]) == 150, "50-cent total preserved"),
        (pricing.cart_total([(99, 3), (1, 3)]) == 300, "mixed cart exact"),
        (pricing.cart_total([]) == 0, "empty cart"),
        (pricing.line_total(199, 2) == 398, "line_total unchanged"),
        (pricing.apply_discount(1000, 10) == 900, "discount unchanged"),
        # hidden cross-module contracts (cart.py / tax.py behaviour)
        (pricing.cart_total([(100, 2)]) + pricing.apply_discount(0, 0) == 200,
         "cart+discount composition"),
        (pricing.cart_total([(250, 4)]) == 1000, "bulk total exact"),
    ]
    bad = [name for ok, name in checks if not ok]
    if bad:
        return False, "contract failures: %s" % ", ".join(bad)
    return True, "%d/%d contracts hold" % (len(checks), len(checks))


def probe_parser(workspace: Path) -> tuple[bool, str]:
    """Off-by-one fixed across shapes (statistical batch counts)."""
    try:
        parser = _load_module(workspace, "parser", "parser.py")
    except Exception as exc:
        return False, "import failed: %r" % (exc,)
    cases = [
        ("a\nb\n\nc\nd", [["a", "b"], ["c", "d"]]),
        ("only", [["only"]]),
        ("", []),
        ("x\n\ny\n\nz", [["x"], ["y"], ["z"]]),
        ("a\n\n\nb", [["a"], ["b"]]),
    ]
    for text, want in cases:
        try:
            got = parser.parse_records(text)
        except Exception as exc:  # noqa: BLE001
            return False, "raised %r on %r" % (exc, text)
        if got != want:
            return False, "drift on %r: got %r" % (text, got)
    # statistical: 1..12 batches always fully preserved
    for n in range(1, 13):
        text = "\n\n".join("r%d" % i for i in range(n))
        if parser.parse_records(text) != [["r%d" % i] for i in range(n)]:
            return False, "dropped batch at n=%d" % n
    return True, "all shapes + 12 statistical batch counts preserved"


PROBES = {
    "lock_ordering": probe_lock_ordering,
    "api_drift": lambda ws, trials=STAT_TRIALS: probe_pricing_contracts(ws),
    "bait_guard": lambda ws, trials=STAT_TRIALS: probe_parser(ws),
}


class ReviewerSuite(SuiteAdapter):
    """Surgical-debug review tasks with bait preservation."""

    suite_name = "reviewer"
    TASK_IDS = ("lock_ordering", "api_drift", "bait_guard")

    def describe_task(self, task_id: str) -> dict[str, Any]:
        meta = REVIEWER_TASKS[task_id]
        return {"task_id": task_id, "title": meta["title"], "brief": meta["brief"]}

    def prepare_task(self, task_id: str, workspace_dir: Path) -> None:
        meta = REVIEWER_TASKS[task_id]
        (workspace_dir / "TASK.md").write_text(
            "# %s\n\n%s\n" % (meta["title"], meta["brief"]), encoding="utf-8"
        )
        target = workspace_dir / meta["target"]
        if not target.exists():
            target.write_text(meta["buggy"], encoding="utf-8")
        bait = workspace_dir / meta["bait"]
        if not bait.exists():
            bait.write_text(meta["bait_src"], encoding="utf-8")

    def build_prompt(self, task_id: str, workspace_dir: Path) -> str:
        meta = REVIEWER_TASKS[task_id]
        return (
            "You are fixing a real bug with a minimal patch: %s.\n\n%s\n\n"
            "Rules: make the SMALLEST edit that fixes the defect in `%s`; "
            "do not rewrite whole functions; never modify `%s` (it is "
            "correct). Verify with `bash` (e.g. run a quick python check). "
            "When done, reply with no further tool calls."
            % (meta["title"], meta["brief"], meta["target"], meta["bait"])
        )

    def evaluate_task(
        self,
        task_id: str,
        workspace_dir: Path,
        trajectory: AgentTrajectory,  # noqa: ARG002 - hook signature
    ) -> tuple[list[MilestoneResult], dict[str, Any]]:
        meta = REVIEWER_TASKS[task_id]
        analyzer = AstDiffAnalyzer()

        # fix milestone (statistical probe; lock task uses barrier + trials)
        if task_id == "lock_ordering":
            fix_ok, fix_detail = probe_lock_ordering(workspace_dir)
        elif task_id == "api_drift":
            fix_ok, fix_detail = probe_pricing_contracts(workspace_dir)
        else:
            fix_ok, fix_detail = probe_parser(workspace_dir)

        # contracts milestone: hidden cross-module checks re-run independently
        if task_id == "api_drift":
            contracts_ok, contracts_detail = probe_pricing_contracts(workspace_dir)
        elif task_id == "lock_ordering":
            contracts_ok, contracts_detail = self._ledger_contracts(workspace_dir)
        else:
            contracts_ok, contracts_detail = probe_parser(workspace_dir)

        # minimal-edit milestone (AST penalty vs the shipped buggy original)
        try:
            after = (workspace_dir / meta["target"]).read_text(encoding="utf-8")
            diff = analyzer.analyze(meta["buggy"], after)
            minimal_ok = diff.penalty >= AST_PENALTY_FLOOR and not diff.syntax_error
            minimal_detail = "penalty=%.3f nodes=%d lines+%d/-%d" % (
                diff.penalty, diff.changed_nodes, diff.lines_added, diff.lines_deleted,
            )
            penalty = diff.penalty
        except OSError as exc:
            minimal_ok, minimal_detail, penalty = False, "unreadable: %s" % exc, 0.1

        # bait milestone (byte-identical preservation)
        try:
            bait_ok = (workspace_dir / meta["bait"]).read_text(
                encoding="utf-8"
            ) == meta["bait_src"]
            bait_detail = "bait intact" if bait_ok else "bait file was modified"
        except OSError as exc:
            bait_ok, bait_detail = False, "bait unreadable: %s" % exc

        milestones = [
            mk_milestone(f"{task_id}_fix", "Defect fixed (statistical probe)",
                         fix_ok, failure_reason=None if fix_ok else fix_detail),
            mk_milestone(f"{task_id}_contracts", "Cross-module contracts hold",
                         contracts_ok, failure_reason=None if contracts_ok else contracts_detail),
            mk_milestone(f"{task_id}_minimal", "Surgical patch (AST penalty>=%.2f)" % AST_PENALTY_FLOOR,
                         minimal_ok, failure_reason=None if minimal_ok else minimal_detail),
            mk_milestone(f"{task_id}_bait", "Bait file preserved",
                         bait_ok, failure_reason=None if bait_ok else bait_detail),
        ]
        return milestones, {"ast_diff_penalty": penalty}

    @staticmethod
    def _ledger_contracts(workspace_dir: Path) -> tuple[bool, str]:
        """Sequential ledger semantics (no lost updates, audit complete)."""
        try:
            ledger = _load_module(workspace_dir, "ledger_contract", "ledger.py")
        except Exception as exc:
            return False, "import failed: %r" % (exc,)
        ledger.reset()
        for _ in range(50):
            ledger.deposit(3)
        for _ in range(20):
            ledger.withdraw(5)
        if ledger.balance["value"] != 50 * 3 - 20 * 5:
            return False, "sequential balance wrong: %r" % (ledger.balance,)
        if len(ledger.audit_log) != 70:
            return False, "audit log incomplete: %d entries" % len(ledger.audit_log)
        return True, "sequential semantics + audit intact"


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    from benchmark_v3.bench_harness.suites.base import ScriptedDriver

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} reviewer::{name}", flush=True)

    suite = ReviewerSuite()
    check("task_ids", suite.task_ids() == ["lock_ordering", "api_drift", "bait_guard"])

    # -- buggy fixtures FAIL the fix probe; reference fixes PASS all four --
    for task_id, meta in REVIEWER_TASKS.items():
        with tempfile.TemporaryDirectory(prefix="rev-buggy-") as tmp:
            ws = Path(tmp)
            (ws / meta["target"]).write_text(meta["buggy"], encoding="utf-8")
            (ws / meta["bait"]).write_text(meta["bait_src"], encoding="utf-8")
            milestones, _ = suite.evaluate_task(task_id, ws, None)  # type: ignore[arg-type]
            by_id = {m.milestone_id: m for m in milestones}
            check("%s_buggy_fix_fails" % task_id, not by_id["%s_fix" % task_id].passed)
            check("%s_buggy_bait_holds" % task_id, by_id["%s_bait" % task_id].passed)
        with tempfile.TemporaryDirectory(prefix="rev-fixed-") as tmp:
            ws = Path(tmp)
            (ws / meta["target"]).write_text(meta["fixed"], encoding="utf-8")
            (ws / meta["bait"]).write_text(meta["bait_src"], encoding="utf-8")
            milestones, extras = suite.evaluate_task(task_id, ws, None)  # type: ignore[arg-type]
            n_pass = sum(1 for m in milestones if m.passed)
            check("%s_fixed_%d_of_4" % (task_id, n_pass), n_pass == 4)
            check("%s_penalty_surgical" % task_id, extras["ast_diff_penalty"] >= AST_PENALTY_FLOOR)

    # -- rewrite-all is penalised below the floor --
    with tempfile.TemporaryDirectory(prefix="rev-rewrite-") as tmp:
        ws = Path(tmp)
        meta = REVIEWER_TASKS["bait_guard"]
        rewrite = (
            "def parse_records(text):\n"
            "    import re\n"
            "    out = []\n"
            "    for chunk in re.split(r'\\n\\n+', text):\n"
            "        lines = [l.strip() for l in chunk.splitlines()]\n"
            "        lines = [l for l in lines if l]\n"
            "        if lines:\n"
            "            entry = list(lines)\n"
            "            out.append(entry)\n"
            "    return out\n"
        )
        (ws / meta["target"]).write_text(rewrite, encoding="utf-8")
        (ws / meta["bait"]).write_text(meta["bait_src"], encoding="utf-8")
        milestones, extras = suite.evaluate_task("bait_guard", ws, None)  # type: ignore[arg-type]
        by_id = {m.milestone_id: m for m in milestones}
        check("rewrite_fix_passes", by_id["bait_guard_fix"].passed)
        check("rewrite_penalised", not by_id["bait_guard_minimal"].passed)

    # -- bait modification fails the bait milestone --
    with tempfile.TemporaryDirectory(prefix="rev-bait-") as tmp:
        ws = Path(tmp)
        meta = REVIEWER_TASKS["lock_ordering"]
        (ws / meta["target"]).write_text(meta["fixed"], encoding="utf-8")
        (ws / meta["bait"]).write_text(meta["bait_src"] + "\n# touched\n", encoding="utf-8")
        milestones, _ = suite.evaluate_task("lock_ordering", ws, None)  # type: ignore[arg-type]
        by_id = {m.milestone_id: m for m in milestones}
        check("bait_touch_fails", not by_id["lock_ordering_bait"].passed)

    # -- full run_session integration (scripted minimal patch via edit tool) --
    with tempfile.TemporaryDirectory(prefix="rev-sess-") as tmp:
        meta = REVIEWER_TASKS["bait_guard"]
        driver = ScriptedDriver(
            "scripted",
            script=[
                {
                    "content": "applying minimal patch",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "name": "edit",
                            "arguments": {
                                "path": "parser.py",
                                "old": "    return records[:-1] if len(records) > 1 else records",
                                "new": "    return records",
                            },
                        }
                    ],
                },
                {"content": "done"},
            ],
        )
        report = suite.run_session("bait_guard", "scripted", driver, tmp)
        check("session_passes", report.passed)
        check("session_penalty", report.ast_diff_penalty >= AST_PENALTY_FLOOR)

    # runner import sanity (used by future shell-based probes)
    check("runner_available", ProcessRunner(default_timeout=5) is not None)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"reviewer self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
