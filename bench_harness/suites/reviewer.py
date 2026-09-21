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

All probes execute **out-of-process** in a child Python interpreter
(:class:`~benchmark_v3.bench_harness.core.runner.ProcessRunner`, one JSON
object on stdout): a model deliverable that calls ``sys.exit`` /
``os._exit`` or loops forever at import time fails its probe — it can
neither kill nor hang the harness parent.
"""

from __future__ import annotations

import hashlib
import json
import sys
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
STAT_TRIALS = 100

#: Per-probe child timeout (seconds). A hung child is a functional failure.
PROBE_TIMEOUT = 60.0


def probe_pricing_fix(workspace: Path, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Targeted fix test: .50 cent-losing special case (out-of-process)."""
    return _run_probe_in_child(workspace, "pricing_fix", timeout=timeout)


def probe_pricing_contracts(workspace: Path, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Preserved semantics: line_total, apply_discount, cart (out-of-process)."""
    return _run_probe_in_child(workspace, "pricing_contracts", timeout=timeout)


def probe_parser_fix(workspace: Path, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Targeted fix test: last record batch is never dropped (out-of-process)."""
    return _run_probe_in_child(workspace, "parser_fix", timeout=timeout)


def probe_parser_contracts(workspace: Path, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Single batch, empty input, consecutive newlines (out-of-process)."""
    return _run_probe_in_child(workspace, "parser_contracts", timeout=timeout)


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
            "Preserve the module's existing public surface exactly: the "
            "`deposit`/`withdraw` signatures (including `_barrier`), the "
            "`balance` dict with its `\"value\"` key, the `audit_log` list, "
            "and `reset()`. Keep the audit behaviour (one entry per "
            "deposit/withdraw). Do NOT modify `audit_util.py`."
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
            "`apply_discount` semantics must be preserved, and existing "
            "pricing contracts (item aggregation, discounts) must keep passing. "
            "Do NOT modify `tax_table.py`."
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
# Probes: out-of-process engine (child interpreter + JSON back-pattern)
# ---------------------------------------------------------------------------
#
# The model's deliverable is arbitrary code: a top-level ``sys.exit()`` /
# ``os._exit()`` or an infinite loop at import time must never kill or hang
# the harness. Every probe therefore runs inside a child Python interpreter
# (via :class:`ProcessRunner`) that loads the workspace module, executes the
# probe body and prints one JSON object on stdout. The parent takes the last
# ``{...}`` line; timeout / non-JSON / crash all degrade to a failed probe.

#: Child preamble: argv is ``<workspace>``; the child defines the JSON sink
#: (``out`` / ``_finish``) and the workspace-module loader used by every body.
_CHILD_PREAMBLE = (
    "import json, os, sys, threading, time\n"
    "ws = sys.argv[1]\n"
    "sys.path.insert(0, ws)\n"
    "out = {'passed': False, 'detail': ''}\n"
    "def _finish():\n"
    "    print(json.dumps(out), flush=True)\n"
    "\n"
    "def _load_module(name, filename):\n"
    "    import importlib.util\n"
    "    spec = importlib.util.spec_from_file_location(\n"
    "        'reviewee_%s_%d' % (name, time.monotonic_ns()),\n"
    "        ws + os.sep + filename)\n"
    "    module = importlib.util.module_from_spec(spec)\n"
    "    spec.loader.exec_module(module)\n"
    "    return module\n"
)


def _run_probe_in_child(
    workspace: Path, probe: str, timeout: float = PROBE_TIMEOUT
) -> tuple[bool, str]:
    """Execute *probe* in a child interpreter; never raises into the harness.

    Returns ``(passed, detail)``. Timeout, crash (``sys.exit`` / ``os._exit``
    / signal) and non-JSON output all degrade to a failed probe with a
    diagnostic string — the parent always survives.
    """
    body = _PROBE_BODIES.get(probe)
    if body is None:
        return False, "unknown probe: %s" % probe
    script = _CHILD_PREAMBLE + body
    runner = ProcessRunner(default_timeout=timeout)
    result = runner.run(
        [sys.executable, "-c", script, str(Path(workspace).resolve())],
        cwd=workspace,
        timeout=timeout,
    )
    if result.timed_out:
        return False, "probe timed out after %.0fs (hang treated as failure)" % timeout
    payload: dict | None = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(payload, dict):
        tail = ((result.stdout or "") + " | " + (result.stderr or ""))[-400:]
        return False, "probe crashed without JSON (rc=%s): %s" % (result.returncode, tail)
    return bool(payload.get("passed")), str(payload.get("detail", ""))


# -- per-probe child bodies (appended to _CHILD_PREAMBLE; each ends with a
#    single ``print(json.dumps(out))`` line that the parent parses) ---------

_PROBE_BODIES: dict[str, str] = {}

_PROBE_BODIES["lock_ordering"] = '''
import os
try:
    ledger = _load_module("ledger", "ledger.py")
except BaseException as exc:
    out["detail"] = "import failed: %r" % (exc,)
    _finish()
    raise SystemExit(0)

# 1. Adversarial probe: barrier forces the racy interleave deterministically.
barrier = threading.Barrier(2)
errors = []

def _run(fn, amount):
    try:
        fn(amount, _barrier=barrier)
    except threading.BrokenBarrierError:
        pass  # forcing barrier did not engage; statistical probe decides
    except BaseException as exc:
        errors.append(repr(exc))

t1 = threading.Thread(target=_run, args=(ledger.deposit, 5), daemon=True)
t2 = threading.Thread(target=_run, args=(ledger.withdraw, 5), daemon=True)
t1.start()
t2.start()
t1.join(timeout=10)
t2.join(timeout=10)
if t1.is_alive() or t2.is_alive():
    out["detail"] = "deadlock under forced interleave (barrier probe)"
    _finish()
    raise SystemExit(0)
if errors:
    out["detail"] = "probe raised: %s" % errors[0]
    _finish()
    raise SystemExit(0)
# 2. Statistical probe: N mixed trials must all settle quickly.
for trial in range(TRIALS):
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
        out["detail"] = "deadlock on statistical trial %d/%d" % (trial + 1, TRIALS)
        _finish()
        raise SystemExit(0)
    if ledger.balance["value"] != 0:
        out["detail"] = "lost update on trial %d" % (trial + 1)
        _finish()
        raise SystemExit(0)
out["passed"] = True
out["detail"] = "%d/%d statistical trials clean" % (TRIALS, TRIALS)
_finish()
'''.replace("TRIALS", str(STAT_TRIALS))

_PROBE_BODIES["pricing_fix"] = '''
import os
try:
    pricing = _load_module("pricing_fix", "pricing.py")
except BaseException as exc:
    out["detail"] = "import failed: %r" % (exc,)
    _finish()
    raise SystemExit(0)
checks = [
    (pricing.cart_total([(150, 1)]) == 150, "50-cent single-item total preserved"),
    (pricing.cart_total([(250, 1)]) == 250, "250-cent total preserved"),
    (pricing.cart_total([(50, 1)]) == 50, "50-cent minimal item preserved"),
    (pricing.cart_total([(99, 3), (1, 3)]) == 300, "mixed-item exact calculation"),
]
bad = [name for ok, name in checks if not ok]
if bad:
    out["detail"] = "fix failures: %s" % ", ".join(bad)
    _finish()
    raise SystemExit(0)
out["passed"] = True
out["detail"] = "%d/%d rounding tests passed" % (len(checks), len(checks))
_finish()
'''

_PROBE_BODIES["pricing_contracts"] = '''
import os
try:
    pricing = _load_module("pricing_contracts", "pricing.py")
except BaseException as exc:
    out["detail"] = "import failed: %r" % (exc,)
    _finish()
    raise SystemExit(0)
checks = [
    (pricing.cart_total([]) == 0, "empty cart returns 0"),
    (pricing.line_total(199, 2) == 398, "line_total unchanged"),
    (pricing.line_total(0, 5) == 0, "line_total zero price"),
    (pricing.apply_discount(1000, 10) == 900, "discount unchanged"),
    (pricing.apply_discount(500, 0) == 500, "zero discount unchanged"),
    (pricing.cart_total([(100, 2)]) + pricing.apply_discount(0, 0) == 200, "cart+discount composition"),
    (pricing.cart_total([(250, 4)]) == 1000, "bulk total exact"),
]
bad = [name for ok, name in checks if not ok]
if bad:
    out["detail"] = "contract failures: %s" % ", ".join(bad)
    _finish()
    raise SystemExit(0)
out["passed"] = True
out["detail"] = "%d/%d contracts hold" % (len(checks), len(checks))
_finish()
'''

_PROBE_BODIES["parser_fix"] = '''
import os
try:
    parser = _load_module("parser_fix", "parser.py")
except BaseException as exc:
    out["detail"] = "import failed: %r" % (exc,)
    _finish()
    raise SystemExit(0)
# Multi-batch inputs where the bug manifests (buggy version drops last batch)
cases = [
    ("a\\nb\\n\\nc\\nd", [["a", "b"], ["c", "d"]]),
    ("x\\n\\ny\\n\\nz", [["x"], ["y"], ["z"]]),
    ("batch1\\n\\nbatch2", [["batch1"], ["batch2"]]),
]
for text, want in cases:
    try:
        got = parser.parse_records(text)
    except BaseException as exc:
        out["detail"] = "raised %r on %r" % (exc, text)
        _finish()
        raise SystemExit(0)
    if got != want:
        out["detail"] = "last batch dropped on %r: got %r, want %r" % (text, got, want)
        _finish()
        raise SystemExit(0)
out["passed"] = True
out["detail"] = "multi-batch parsing verified (no batch dropped)"
_finish()
'''

_PROBE_BODIES["parser_contracts"] = '''
import os
try:
    parser = _load_module("parser_contracts", "parser.py")
except BaseException as exc:
    out["detail"] = "import failed: %r" % (exc,)
    _finish()
    raise SystemExit(0)
cases = [
    ("only", [["only"]]),
    ("", []),
    ("   \\n   ", []),
    ("a\\n\\n\\nb", [["a"], ["b"]]),
]
for text, want in cases:
    try:
        got = parser.parse_records(text)
    except BaseException as exc:
        out["detail"] = "raised %r on %r" % (exc, text)
        _finish()
        raise SystemExit(0)
    if got != want:
        out["detail"] = "drift on %r: got %r" % (text, got)
        _finish()
        raise SystemExit(0)
# 1..12 batches preservation
for n in range(1, 13):
    text = "\\n\\n".join("r%d" % i for i in range(n))
    try:
        got = parser.parse_records(text)
    except BaseException as exc:
        out["detail"] = "raised %r at n=%d" % (exc, n)
        _finish()
        raise SystemExit(0)
    if got != [["r%d" % i] for i in range(n)]:
        out["detail"] = "dropped batch at n=%d" % n
        _finish()
        raise SystemExit(0)
out["passed"] = True
out["detail"] = "single-batch and boundary shapes preserved across 1..12 batches"
_finish()
'''

_PROBE_BODIES["ledger_contracts"] = '''
import os
try:
    ledger = _load_module("ledger_contract", "ledger.py")
except BaseException as exc:
    out["detail"] = "import failed: %r" % (exc,)
    _finish()
    raise SystemExit(0)
ledger.reset()
for _ in range(50):
    ledger.deposit(3)
for _ in range(20):
    ledger.withdraw(5)
if ledger.balance["value"] != 50 * 3 - 20 * 5:
    out["detail"] = "sequential balance wrong: %r" % (ledger.balance,)
    _finish()
    raise SystemExit(0)
if len(ledger.audit_log) != 70:
    out["detail"] = "audit log incomplete: %d entries" % len(ledger.audit_log)
    _finish()
    raise SystemExit(0)
out["passed"] = True
out["detail"] = "sequential semantics + audit intact"
_finish()
'''


def probe_lock_ordering(workspace: Path, trials: int = STAT_TRIALS,
                        timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Barrier-forced interleave + statistical runs; True iff never deadlocks.

    Runs entirely in a child interpreter: a buggy ``ledger.py`` cannot kill
    or hang the parent harness (crash → failed probe, hang → timeout).
    """
    _ = trials  # STAT_TRIALS is baked into the child body; kept for API compat
    return _run_probe_in_child(workspace, "lock_ordering", timeout=timeout)


PROBES = {
    "lock_ordering": probe_lock_ordering,
    "api_drift": lambda ws, trials=STAT_TRIALS: probe_pricing_contracts(ws),
    "bait_guard": lambda ws, trials=STAT_TRIALS: probe_parser_contracts(ws),
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
            "Rules: make the SMALLEST edit that fixes the defect in `%s` "
            "with the `write` or `edit` tool; do not rewrite whole "
            "functions; never modify `%s` (it is correct). Verify with "
            "`bash` if useful, but the patched file on disk is what scores. "
            "When the file on disk is ready, call the `finish` tool with a "
            "non-empty summary of what you changed and how you checked it; "
            "a reply with no tool calls does not end the task."
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
            fix_ok, fix_detail = probe_pricing_fix(workspace_dir)
        else:
            fix_ok, fix_detail = probe_parser_fix(workspace_dir)

        # contracts milestone: hidden cross-module checks re-run independently
        if task_id == "api_drift":
            contracts_ok, contracts_detail = probe_pricing_contracts(workspace_dir)
        elif task_id == "lock_ordering":
            contracts_ok, contracts_detail = self._ledger_contracts(workspace_dir)
        else:
            contracts_ok, contracts_detail = probe_parser_contracts(workspace_dir)

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
        """Sequential ledger semantics (no lost updates, audit complete).

        Out-of-process: loads ``ledger.py`` inside the child so a poisoned
        module cannot damage the harness.
        """
        return _run_probe_in_child(workspace_dir, "ledger_contracts")


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    from benchmark_v3.bench_harness.suites.base import ScriptedDriver, scripted_finish

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
            "def _clean_str(s):\n"
            "    return s.strip()\n\n"
            "def _split_chunks(text):\n"
            "    return [c for c in text.split(chr(10) + chr(10)) if c.strip()]\n\n"
            "def parse_records(text):\n"
            "    records = []\n"
            "    for chunk in _split_chunks(text):\n"
            "        batch = []\n"
            "        for raw_line in chunk.splitlines():\n"
            "            item = _clean_str(raw_line)\n"
            "            if len(item) > 0:\n"
            "                batch.append(item)\n"
            "        if len(batch) > 0:\n"
            "            records.append(batch)\n"
            "    return records\n"
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
                scripted_finish("Patched parser.py last-batch drop; left utils_codec.py untouched."),
            ],
        )
        report = suite.run_session("bait_guard", "scripted", driver, tmp)
        check("session_passes", report.passed)
        check("session_penalty", report.ast_diff_penalty >= AST_PENALTY_FLOOR)

    # runner import sanity (used by future shell-based probes)
    check("runner_available", ProcessRunner(default_timeout=5) is not None)

    # -- crash-proof containment: a malicious/poisoned deliverable must not --
    # -- kill or hang the parent harness (probes are out-of-process now)    --
    meta_bait_guard = REVIEWER_TASKS["bait_guard"]
    with tempfile.TemporaryDirectory(prefix="rev-crash-") as tmp:
        ws = Path(tmp)
        # 1. os._exit(1) at import time: hard-crashes the child only.
        (ws / "parser.py").write_text("import os\nos._exit(1)\n", encoding="utf-8")
        (ws / meta_bait_guard["bait"]).write_text(meta_bait_guard["bait_src"], encoding="utf-8")
        ok, detail = probe_parser_contracts(ws, timeout=15.0)
        check("child_os_exit_survives", ok is False and "rc=1" in detail or "crashed" in detail or "JSON" in detail)
        ok2, detail2 = probe_parser_fix(ws, timeout=15.0)
        check("child_os_exit_fix_fails_only", ok2 is False)
        # 2. sys.exit at import time: SystemExit inside the child.
        (ws / "parser.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        ok3, detail3 = probe_parser_contracts(ws, timeout=15.0)
        check("child_sys_exit_survives", ok3 is False and (
            "SystemExit" in detail3 or "rc=3" in detail3
        ))
    with tempfile.TemporaryDirectory(prefix="rev-hang-") as tmp:
        ws = Path(tmp)
        # 3. top-level infinite loop: the child must hit the timeout, the
        #    parent must survive and report the hang as a failed probe.
        (ws / "parser.py").write_text("while True:\n    pass\n", encoding="utf-8")
        (ws / meta_bait_guard["bait"]).write_text(meta_bait_guard["bait_src"], encoding="utf-8")
        t0 = time.monotonic()
        ok4, detail4 = probe_parser_contracts(ws, timeout=4.0)
        dt = time.monotonic() - t0
        check("child_hang_times_out", ok4 is False and "timed out" in detail4 and dt < 30.0)
    with tempfile.TemporaryDirectory(prefix="rev-exit-") as tmp:
        ws = Path(tmp)
        # 4. print-noise + late os._exit: parent takes the LAST {...} line;
        #    a poisoned module that prints junk still degrades cleanly.
        (ws / "pricing.py").write_text(
            'print("garbage")\nimport os\nos._exit(1)\n', encoding="utf-8"
        )
        ok5, detail5 = probe_pricing_contracts(ws, timeout=15.0)
        check("child_noise_then_exit_degrades", ok5 is False)
        check("parent_still_alive", True)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"reviewer self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
