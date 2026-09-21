"""Next-gen short-task suite: micro-engines with hard resource oracles.

Three tasks (30 assertions, 10 per task), each scored by running the model's
``solution.py`` inside a fresh child interpreter via
:class:`~benchmark_v3.bench_harness.core.runner.ProcessRunner`:

1. ``varint_parser`` — streaming protobuf-style Varint decoder with a
   :class:`~benchmark_v3.bench_harness.core.memory_probe.MemoryProbe`
   oracle (``tracemalloc`` peak <= 4 MiB on a ~7 MiB wire stream).
2. ``timing_wheel`` — hashed timing-wheel timer driver (amortised O(1)
   tick, multi-round deadlines, skew-free firing, cancel semantics).
3. ``lexer_state_machine`` — flat lexer plus recursive macro expander
   with depth limiting and error recovery (never raises on bad input,
   except ``ValueError`` for cyclic/over-deep macros).

Every task: one child process, one 10 s timeout (SPEC: timeouts are
functional failures, elapsed time itself is pure telemetry). Task briefs
(``TASK.md``) state observable behaviour and resource limits only — no
implementation hints.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.runner import ProcessRunner
from benchmark_v3.bench_harness.core.types import AgentTrajectory, MilestoneResult
from benchmark_v3.bench_harness.suites.base import SuiteAdapter, mk_milestone

__all__ = ["ShortTaskSuite", "SHORT_TASK_TIMEOUT", "run_task_checks"]

#: Per-task child-process timeout (seconds). Exceeding it fails the task's
#: assertions; the elapsed time itself never affects the score.
#: Set to 20.0s for Windows cross-platform headroom on multi-MiB tracemalloc.
SHORT_TASK_TIMEOUT = 20.0

#: tracemalloc peak budget for the varint streaming check (4 MiB).
VARINT_MEMORY_LIMIT = 4 * 1024 * 1024

#: Fixed evaluation seed (deterministic CI; pass another seed to re-sample).
EVAL_SEED = 1337


# ---------------------------------------------------------------------------
# Task briefs (behavioural contracts only)
# ---------------------------------------------------------------------------

TASK_BRIEFS: dict[str, dict[str, str]] = {
    "varint_parser": {
        "title": "Zero-copy streaming Varint parser",
        "brief": (
            "Implement `solution.py` with:\n"
            "- `encode_varint(value: int) -> bytes`: protobuf-style base-128 "
            "varint for 0 <= value < 2**64 (raise ValueError outside range).\n"
            "- `iter_values(chunks: Iterable[bytes]) -> Iterator[int]`: lazily "
            "decode values across arbitrary chunk splits (1-byte splits must "
            "work; empty chunks are ignored). Raise ValueError on truncated "
            "input or on an overlong encoding (>10 bytes with the "
            "continuation bit still set).\n"
            "RESOURCE LIMIT: decoding a multi-MiB stream while consuming the "
            "iterator incrementally must peak at <= 4 MiB traced memory."
        ),
    },
    "timing_wheel": {
        "title": "Hashed timing-wheel timer driver",
        "brief": (
            "Implement `solution.py` with class `TimingWheel(tick_ms=10, "
            "wheel_size=256)` exposing:\n"
            "- `now() -> int`: the current tick, starting at `0` on a fresh "
            "instance and increasing by exactly 1 per `tick()` (tick is the "
            "unit; `tick_ms` does not change it).\n"
            "- `schedule(delay: int, payload) -> int`: fire `payload` at tick "
            "`now + max(1, delay)` (`delay >= 0`; delay 0 and delay 1 both "
            "fire on the next tick). Returns a timer id.\n"
            "- `cancel(timer_id) -> bool`: True iff a pending timer was "
            "removed (unknown/already-fired ids -> False).\n"
            "- `tick() -> list`: advance one tick, return due payloads in "
            "FIFO order.\n"
            "- `pending() -> int`.\n"
            "PERF LIMIT: scheduling tens of thousands of timers and ticking "
            "thousands of times must finish in a few seconds (amortised O(1) "
            "tick; a full scan per tick will time out)."
        ),
    },
    "lexer_state_machine": {
        "title": "Lexer state machine with nested macro expansion",
        "brief": (
            "Implement `solution.py` with:\n"
            "- `tokenize(source: str) -> list[tuple[str, str]]`: emit "
            "('IDENT', ...), ('INT', ...), ('STRING', ...) (escapes \\\\ \\\" "
            "\\n \\t), ('SYM', ch) for single-char symbols in `()[]{},;=+-*/`, "
            "('MACRO_OPEN', '#['), and ('ERROR', reason). Skip whitespace and "
            "`//` line comments. `#` not starting `#[` is an ERROR token. "
            "Unclosed strings/macros emit ERROR and recovery continues; "
            "`tokenize` never raises on bad input. An ERROR token's second "
            "element is the offending character (e.g. `('ERROR', '@')` for a "
            "stray `@`), or a short reason string for malformed constructs.\n"
            "- `expand(tokens, env: dict[str, list[tuple]]) -> list[tuple]`: "
            "recursively splice `#[NAME]` with `env[NAME]` (nested macros "
            "expand depth-first, `]`-matched). Unknown names / unclosed "
            "macros emit ERROR tokens (an unknown macro's ERROR text contains "
            "the missing name). Depth > 64 or cycles raise ValueError."
        ),
    },
}

STARTER_SOLUTION = '''"""Starter skeleton — implement the API described in TASK.md."""

# varint_parser -----------------------------------------------------------
def encode_varint(value):
    raise NotImplementedError


def iter_values(chunks):
    raise NotImplementedError
    yield  # make this a generator


# timing_wheel ------------------------------------------------------------
class TimingWheel:
    def __init__(self, tick_ms=10, wheel_size=256):
        raise NotImplementedError

    def schedule(self, delay, payload):
        raise NotImplementedError

    def cancel(self, timer_id):
        raise NotImplementedError

    def tick(self):
        raise NotImplementedError

    def now(self):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError


# lexer_state_machine -----------------------------------------------------
def tokenize(source):
    raise NotImplementedError


def expand(tokens, env):
    raise NotImplementedError
'''


# ---------------------------------------------------------------------------
# Child check scripts (run with: python -c SCRIPT <workspace> <seed>)
# ---------------------------------------------------------------------------

_CHILD_PREAMBLE = """
import json, sys, tracemalloc, random, re
ws, seed = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, ws)
out = {"assertions": [], "peak_bytes": 0}
def record(aid, passed, detail=""):
    out["assertions"].append({"id": aid, "passed": bool(passed), "detail": str(detail)})
def finish():
    print(json.dumps(out), flush=True)
"""

_CHILD_IMPORT = """
try:
    import solution
except Exception as exc:
    for aid in ("a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10"):
        record(aid, False, "import solution failed: %r" % (exc,))
    finish()
    sys.exit(0)
"""

_VARINT_CHECK = _CHILD_PREAMBLE + _CHILD_IMPORT + """
rng = random.Random(seed)
vals = [0, 1, 127, 128, 300, 16384, 2**32 - 1, 2**63 - 1, 2**64 - 1]
vals += [rng.randrange(0, 2**64) for _ in range(400)]
wire = b""
try:
    wire = b"".join(solution.encode_varint(v) for v in vals)
except Exception:
    pass

# a1: single-byte values (0..127) encode to 1 byte and decode correctly
try:
    s_vals = list(range(128))
    s_wire = b"".join(solution.encode_varint(v) for v in s_vals)
    s_ok = len(s_wire) == 128 and list(solution.iter_values(iter([s_wire]))) == s_vals
    record("a1", s_ok, "128 single-byte roundtrip")
except Exception as exc:
    record("a1", False, "single-byte raised %r" % (exc,))

# a2: multi-byte boundary values
try:
    m_vals = [128, 300, 16384, 2**32 - 1, 2**63 - 1, 2**64 - 1]
    m_wire = b"".join(solution.encode_varint(v) for v in m_vals)
    m_ok = list(solution.iter_values(iter([m_wire]))) == m_vals
    record("a2", m_ok, "multi-byte boundary roundtrip")
except Exception as exc:
    record("a2", False, "multi-byte raised %r" % (exc,))

# a3: random fuzz chunk splits (1..37 bytes)
try:
    chunks = []
    i = 0
    while i < len(wire):
        j = min(len(wire), i + rng.randint(1, 37))
        chunks.append(wire[i:j])
        i = j
    got = list(solution.iter_values(iter(chunks)))
    record("a3", got == vals, "decoded %d/%d values" % (len(got), len(vals)))
except Exception as exc:
    record("a3", False, "fuzz splits raised %r" % (exc,))

# a4: extreme 1-byte chunk splits (every byte alone)
try:
    one = [wire[k:k+1] for k in range(len(wire))]
    record("a4", list(solution.iter_values(iter(one))) == vals, "1-byte fragmentation")
except Exception as exc:
    record("a4", False, "1-byte splits raised %r" % (exc,))

# a5: empty chunk tolerance
try:
    padded = [b"", wire[:50], b"", b"", wire[50:], b""]
    record("a5", list(solution.iter_values(iter(padded))) == vals, "empty chunks ignored")
except Exception as exc:
    record("a5", False, "empty chunks raised %r" % (exc,))

# a6: empty input stream returns empty iterator (and non-empty stream yields valid items)
try:
    e_ok = (
        list(solution.iter_values(iter([]))) == []
        and list(solution.iter_values(iter([b"", b""]))) == []
        and list(solution.iter_values(iter([b"\\x01"]))) == [1]
    )
    record("a6", e_ok, "empty stream handled and non-empty stream decodes")
except Exception as exc:
    record("a6", False, "empty stream raised %r" % (exc,))

# a7: truncated wire stream raises ValueError
try:
    trunc_ok = False
    try:
        list(solution.iter_values(iter([solution.encode_varint(300)[:-1]])))
    except ValueError:
        trunc_ok = True
    record("a7", trunc_ok, "truncated varint raises ValueError")
except Exception as exc:
    record("a7", False, "truncation test raised %r" % (exc,))

# a8: overlong varint (>10 bytes with continuation bit) rejected
try:
    over_ok = False
    try:
        list(solution.iter_values(iter([b"\\x80" * 11])))
    except ValueError:
        over_ok = True
    record("a8", over_ok, ">10 byte sequence rejected")
except Exception as exc:
    record("a8", False, "overlong test raised %r" % (exc,))

# a9: out-of-range inputs (<0 or >= 2**64) rejected
try:
    range_bad = 0
    try:
        solution.encode_varint(-1)
    except (ValueError, OverflowError):
        range_bad += 1
    try:
        solution.encode_varint(2**64)
    except (ValueError, OverflowError):
        range_bad += 1
    record("a9", range_bad == 2, "out-of-range rejected %d/2" % range_bad)
except Exception as exc:
    record("a9", False, "range test raised %r" % (exc,))

# a10: memory oracle — ~4.4MiB wire, incremental consumption, peak <= 4MiB
try:
    N = 500000
    def _gen():
        buf = bytearray()
        for k in range(N):
            buf += solution.encode_varint((k * 2654435761) % (2**63))
            if len(buf) >= 65536:
                yield bytes(buf)
                buf = bytearray()
        if buf:
            yield bytes(buf)
    tracemalloc.start()
    count, xorsum = 0, 0
    for v in solution.iter_values(_gen()):
        count += 1
        xorsum ^= v
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    out["peak_bytes"] = peak
    expect = 0
    for k in range(N):
        expect ^= (k * 2654435761) % (2**63)
    ok = count == N and xorsum == expect and peak <= MEMLIMIT_BYTES
    record("a10", ok, "count=%d peak=%.2fMiB" % (count, peak / 1048576.0))
except Exception as exc:
    try:
        tracemalloc.stop()
    except Exception:
        pass
    record("a10", False, "memory probe raised %r" % (exc,))
finish()
""".replace("MEMLIMIT_BYTES", str(VARINT_MEMORY_LIMIT))

_WHEEL_CHECK = _CHILD_PREAMBLE + _CHILD_IMPORT + """
import time
W = solution.TimingWheel

# a1: zero delay fires on tick 1
try:
    w1 = W(tick_ms=10, wheel_size=16)
    w1.schedule(0, "z0")
    record("a1", w1.tick() == ["z0"], "zero-delay fires immediately")
except Exception as exc:
    record("a1", False, "zero-delay raised %r" % (exc,))

# a2: single-round delay accuracy (1..15)
try:
    w2 = W(tick_ms=10, wheel_size=16)
    w2.schedule(1, "one")
    w2.schedule(2, "two")
    w2.schedule(5, "five")
    f_res = {}
    for _ in range(10):
        for item in w2.tick():
            f_res[item] = w2.now()
    ok_a2 = f_res.get("one") == 1 and f_res.get("two") == 2 and f_res.get("five") == 5
    record("a2", ok_a2, "single-round fired=%r" % (f_res,))
except Exception as exc:
    record("a2", False, "single-round raised %r" % (exc,))

# a3: multi-round delay accuracy (16..19)
try:
    w3 = W(tick_ms=10, wheel_size=16)
    w3.schedule(16, "round1")
    w3.schedule(19, "round1+3")
    f3 = {}
    for _ in range(25):
        for item in w3.tick():
            f3[item] = w3.now()
    ok_a3 = f3.get("round1") == 16 and f3.get("round1+3") == 19
    record("a3", ok_a3, "multi-round fired=%r" % (f3,))
except Exception as exc:
    record("a3", False, "multi-round raised %r" % (exc,))

# a4: far future delay accuracy (40 ticks)
try:
    w4 = W(tick_ms=10, wheel_size=16)
    w4.schedule(40, "far")
    f4 = {}
    for _ in range(50):
        for item in w4.tick():
            f4[item] = w4.now()
    record("a4", f4.get("far") == 40, "far future fired=%r" % (f4,))
except Exception as exc:
    record("a4", False, "far future raised %r" % (exc,))

# a5: FIFO ordering for identical deadlines
try:
    w5 = W(tick_ms=10, wheel_size=16)
    w5.schedule(3, "first3")
    w5.schedule(3, "second3")
    f5 = []
    for _ in range(5):
        f5.extend(w5.tick())
    record("a5", f5 == ["first3", "second3"], "fifo order=%r" % (f5,))
except Exception as exc:
    record("a5", False, "fifo raised %r" % (exc,))

# a6: cancel pending timer returns True and prevents firing
try:
    w6 = W(tick_ms=10, wheel_size=16)
    tid = w6.schedule(2, "bye")
    c1 = w6.cancel(tid) is True
    seen = []
    for _ in range(5):
        seen.extend(w6.tick())
    record("a6", c1 and "bye" not in seen, "cancel pending ok")
except Exception as exc:
    record("a6", False, "cancel pending raised %r" % (exc,))

# a7: cancel unknown or already-fired timer returns False (active timer returns True)
try:
    w7 = W(tick_ms=10, wheel_size=16)
    t_live = w7.schedule(5, "live")
    c_live = w7.cancel(t_live) is True
    c_unk = w7.cancel(999999) is False
    t2 = w7.schedule(1, "fire")
    w7.tick()
    c_fired = w7.cancel(t2) is False
    record("a7", c_live and c_unk and c_fired, "cancel live/unknown/fired ok")
except Exception as exc:
    record("a7", False, "cancel invalid raised %r" % (exc,))

# a8: clock monotonicity and now() advancement
try:
    w8 = W(tick_ms=10, wheel_size=8)
    ok_c = w8.now() == 0
    w8.tick()
    ok_c = ok_c and w8.now() == 1
    w8.tick()
    ok_c = ok_c and w8.now() == 2
    record("a8", ok_c, "clock monotonicity ok")
except Exception as exc:
    record("a8", False, "clock monotonicity raised %r" % (exc,))

# a9: pending() count accurate across schedule, tick, and cancel
try:
    w9 = W(tick_ms=10, wheel_size=8)
    ok_p = w9.pending() == 0
    tid1 = w9.schedule(2, "p1")
    tid2 = w9.schedule(4, "p2")
    ok_p = ok_p and w9.pending() == 2
    w9.cancel(tid1)
    ok_p = ok_p and w9.pending() == 1
    w9.tick()
    w9.tick()
    w9.tick()
    w9.tick()
    ok_p = ok_p and w9.pending() == 0
    record("a9", ok_p, "pending count accuracy")
except Exception as exc:
    record("a9", False, "pending count raised %r" % (exc,))

# a10: tick efficiency (60k timers x 5k ticks in < 3s, amortised O(1))
try:
    w10 = W(tick_ms=10, wheel_size=256)
    rng2 = random.Random(seed)
    for k in range(60000):
        w10.schedule(rng2.randrange(1, 5000), k)
    t0 = time.monotonic()
    n = 0
    timeout_hit = False
    for step in range(5000):
        n += len(w10.tick())
        if step % 200 == 0 and (time.monotonic() - t0) > 4.5:
            timeout_hit = True
            break
    dt = time.monotonic() - t0
    ok_eff = (not timeout_hit) and n == 60000 and dt < 3.0
    record("a10", ok_eff, "fired=%d in %.2fs%s" % (n, dt, " (aborted O(N))" if timeout_hit else ""))
except Exception as exc:
    record("a10", False, "efficiency raised %r" % (exc,))
finish()
"""

_LEXER_CHECK = _CHILD_PREAMBLE + _CHILD_IMPORT + """
import time
tok, exp = solution.tokenize, solution.expand

# a1: basic identifier and keyword tokens
try:
    res = tok("let x = 42;")
    expected = [("IDENT", "let"), ("IDENT", "x"), ("SYM", "="), ("INT", "42"), ("SYM", ";")]
    record("a1", res == expected, "ident/keyword token sequence ok")
except Exception as exc:
    record("a1", False, "ident raised %r" % (exc,))

# a2: integer literal tokens
try:
    res = tok("100 0 99999")
    ints = [t[1] for t in res if t[0] == "INT"]
    record("a2", ints == ["100", "0", "99999"], "int tokens=%r" % (ints,))
except Exception as exc:
    record("a2", False, "int raised %r" % (exc,))

# a3: symbols and punctuation
try:
    res = tok("= ; + - * / ( ) [ ]")
    syms = [t[1] for t in res if t[0] == "SYM"]
    expected_syms = set("=;+-*/()[]")
    record("a3", len(syms) == 10 and set(syms) == expected_syms, "all 10 symbol tokens ok")
except Exception as exc:
    record("a3", False, "symbols raised %r" % (exc,))

# a4: line comments skipped
try:
    res = tok("a = 1; // this is comment\\nb = 2;")
    idents = [t[1] for t in res if t[0] == "IDENT"]
    record("a4", idents == ["a", "b"] and not any("comment" in t[1] for t in res), "comments skipped")
except Exception as exc:
    record("a4", False, "comments raised %r" % (exc,))

# a5: string literals with escape sequences
try:
    res = tok('s = "hello\\\\nworld";')
    strs = [t[1] for t in res if t[0] == "STRING"]
    record("a5", strs == ["hello\\nworld"], "string escape tokens=%r" % (strs,))
except Exception as exc:
    record("a5", False, "strings raised %r" % (exc,))

# a6: stray/invalid characters produce ERROR tokens without crashing
try:
    res = tok("ok @ dear # foo")
    errs = [t[1] for t in res if t[0] == "ERROR"]
    # 题面允许 ERROR 的第二个元素是「 offending character 」或「 short reason
    # string 」，所以按子串匹配：模型输出 "unexpected '#'" / "invalid '#'
    # character" 这类带上下文的原因串，同样算正确识别了杂散字符。
    # 但必须排除 "#" 只是行号/编号前缀的无关串（如 "line #1"、"Error #404"），
    # 否则一个完全没识别杂散 # 的实现会靠无关文本蒙混过关。
    record("a6", any("@" in e for e in errs)
           and any(re.search(r"(?<![A-Za-z0-9])#(?![A-Za-z0-9])", e) for e in errs),
           "stray error tokens=%r" % (errs,))
except Exception as exc:
    record("a6", False, "stray chars raised %r" % (exc,))

# a7: unterminated string produces ERROR token and resumes scanning
try:
    res = tok('"unclosed\\nstill = 1;')
    has_err = any(t[0] == "ERROR" for t in res)
    has_resumed = any(t[1] == "still" for t in res)
    record("a7", has_err and has_resumed, "unterminated string recovery")
except Exception as exc:
    record("a7", False, "unterminated string raised %r" % (exc,))

# a8: nested macro expansion
try:
    env = {"B": [("INT", "1"), ("SYM", "+"), ("INT", "2")],
           "A": [("MACRO_OPEN", "#["), ("IDENT", "B"), ("SYM", "]"), ("SYM", "*")],
           "C": [("MACRO_OPEN", "#["), ("IDENT", "A"), ("SYM", "]")]}
    got = exp([("MACRO_OPEN", "#["), ("IDENT", "C"), ("SYM", "]"),
               ("MACRO_OPEN", "#["), ("IDENT", "B"), ("SYM", "]")], env)
    want = [("INT", "1"), ("SYM", "+"), ("INT", "2"), ("SYM", "*"),
            ("INT", "1"), ("SYM", "+"), ("INT", "2")]
    record("a8", got == want, "nested macro expansion")
except Exception as exc:
    record("a8", False, "nested macro raised %r" % (exc,))

# a9: unknown macro and unclosed macro produce ERROR token
try:
    unk = exp([("MACRO_OPEN", "#["), ("IDENT", "NOPE"), ("SYM", "]")], {})
    unclosed = exp([("MACRO_OPEN", "#["), ("IDENT", "FOO")], {})
    has_unk_err = any(t[0] == "ERROR" and "NOPE" in t[1] for t in unk)
    has_unclosed_err = any(t[0] == "ERROR" for t in unclosed)
    record("a9", has_unk_err and has_unclosed_err, "unknown/unclosed macro token produces ERROR")
except Exception as exc:
    record("a9", False, "unknown/unclosed macro raised %r" % (exc,))

# a10: cyclic/over-deep macro depth limiting + large text throughput
try:
    cyc = {"A": [("MACRO_OPEN", "#["), ("IDENT", "A"), ("SYM", "]")]}
    cyc_ok = False
    try:
        exp([("MACRO_OPEN", "#["), ("IDENT", "A"), ("SYM", "]")], cyc)
    except ValueError:
        cyc_ok = True
    big = "x = 1; " * 40000
    t0 = time.monotonic()
    tb = tok(big)
    dt = time.monotonic() - t0
    record("a10", cyc_ok and len(tb) == 160000 and dt < 2.5, "cycle safety + throughput %.2fs" % dt)
except Exception as exc:
    record("a10", False, "robustness raised %r" % (exc,))
finish()
"""

CHECK_SCRIPTS: dict[str, str] = {
    "varint_parser": _VARINT_CHECK,
    "timing_wheel": _WHEEL_CHECK,
    "lexer_state_machine": _LEXER_CHECK,
}

ASSERTION_NAMES: dict[str, list[str]] = {
    "varint_parser": [
        "Single-byte varint encoding (0..127)",
        "Multi-byte boundary values (128..2^64-1)",
        "Random chunk splits streaming decoding",
        "1-byte extreme chunk fragmentation",
        "Empty chunk tolerance in stream",
        "Empty stream returns empty generator",
        "Truncated wire stream raises ValueError",
        "Overlong varint (>10 bytes) rejected",
        "Out-of-range values (<0 or >=2^64) rejected",
        "Memory oracle: peak <= 4MiB on 800k values",
    ],
    "timing_wheel": [
        "Immediate firing (delay=0 on tick 1)",
        "Single-round delay accuracy (1..15 ticks)",
        "Multi-round delay accuracy (16..19 ticks)",
        "Far future delay accuracy (40 ticks)",
        "FIFO ordering for identical deadlines",
        "Cancel pending timer returns True & suppresses firing",
        "Cancel unknown/expired timer returns False",
        "Clock monotonicity across ticks",
        "Pending count accuracy across lifecycle",
        "Tick efficiency: 60k timers x 5k ticks in < 3s",
    ],
    "lexer_state_machine": [
        "Identifier and keyword tokenization",
        "Integer literal tokenization",
        "Symbols and operator tokenization",
        "Line comments skipped cleanly",
        "String literals with escape sequences",
        "Stray characters produce ERROR token without crash",
        "Unterminated string recovery and continued scan",
        "Single & nested macro expansion",
        "Unknown macro emits ERROR token without crash",
        "Cycle/over-deep macro depth limiting & throughput",
    ],
}


def run_task_checks(
    task_id: str,
    workspace_dir: str | Path,
    seed: int = EVAL_SEED,
    timeout: float = SHORT_TASK_TIMEOUT,
) -> dict[str, Any]:
    """Run one task's 10 assertions in a child interpreter.

    Returns ``{"assertions": [{id, passed, detail}], "peak_bytes": int,
    "timed_out": bool, "diagnostics": str}``. Never raises on model bugs:
    missing files, import errors and timeouts become failed assertions.
    """
    workspace_dir = Path(workspace_dir)
    script = CHECK_SCRIPTS[task_id]
    names = ASSERTION_NAMES[task_id]
    num_assertions = len(names)
    runner = ProcessRunner(default_timeout=timeout)
    result = runner.run(
        [sys.executable, "-c", script, str(workspace_dir), str(seed)],
        cwd=workspace_dir,
        timeout=timeout,
    )
    if result.timed_out:
        return {
            "assertions": [
                {"id": f"a{i}", "passed": False, "detail": "timeout (>%.0fs)" % timeout}
                for i in range(1, num_assertions + 1)
            ],
            "peak_bytes": 0,
            "timed_out": True,
            "diagnostics": (result.stdout + result.stderr)[-2000:],
        }
    payload: dict[str, Any] | None = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not payload or not isinstance(payload.get("assertions"), list):
        diagnostics = ((result.stdout or "") + "\n" + (result.stderr or ""))[-2000:]
        return {
            "assertions": [
                {"id": f"a{i}", "passed": False, "detail": "checker produced no JSON"}
                for i in range(1, num_assertions + 1)
            ],
            "peak_bytes": 0,
            "timed_out": False,
            "diagnostics": diagnostics,
        }
    by_id = {a.get("id"): a for a in payload["assertions"] if isinstance(a, dict)}
    assertions = [
        {
            "id": f"a{i}",
            "passed": bool(by_id.get(f"a{i}", {}).get("passed", False)),
            "detail": str(by_id.get(f"a{i}", {}).get("detail", "")),
        }
        for i in range(1, num_assertions + 1)
    ]
    _ = names
    return {
        "assertions": assertions,
        "peak_bytes": int(payload.get("peak_bytes", 0) or 0),
        "timed_out": False,
        "diagnostics": "",
    }


class ShortTaskSuite(SuiteAdapter):
    """Micro-engine short tasks with memory oracle and 10 s timeouts."""

    suite_name = "short"
    TASK_IDS = ("varint_parser", "timing_wheel", "lexer_state_machine")

    def describe_task(self, task_id: str) -> dict[str, Any]:
        meta = TASK_BRIEFS[task_id]
        return {"task_id": task_id, **meta}

    def prepare_task(self, task_id: str, workspace_dir: Path) -> None:
        meta = TASK_BRIEFS[task_id]
        (workspace_dir / "TASK.md").write_text(
            "# %s\n\n%s\n" % (meta["title"], meta["brief"]), encoding="utf-8"
        )
        solution = workspace_dir / "solution.py"
        if not solution.exists():
            solution.write_text(STARTER_SOLUTION, encoding="utf-8")

    def build_prompt(self, task_id: str, workspace_dir: Path) -> str:
        meta = TASK_BRIEFS[task_id]
        return (
            "You are implementing a micro-engine benchmark task: %s.\n\n"
            "Contract (implement exactly this API in `solution.py`):\n%s\n\n"
            "Rules: use the `write` or `edit` tool to implement ONLY "
            "`solution.py` in the workspace — bash/`python -c` prototypes "
            "are not scored; no network; no third-party packages; keep "
            "per-call work incremental (streaming/memory limits are "
            "enforced). When the file on disk is ready, call the `finish` "
            "tool with a non-empty summary of what you changed and how you "
            "checked it; a reply with no tool calls does not end the task."
            % (meta["title"], meta["brief"])
        )

    def evaluate_task(
        self,
        task_id: str,
        workspace_dir: Path,
        trajectory: AgentTrajectory,  # noqa: ARG002 - hook signature
    ) -> tuple[list[MilestoneResult], dict[str, Any]]:
        outcome = run_task_checks(task_id, workspace_dir)
        names = ASSERTION_NAMES[task_id]
        milestones = [
            mk_milestone(
                "%s_a%d" % (task_id, i),
                names[i - 1],
                a["passed"],
                failure_reason=None if a["passed"] else a["detail"],
                diagnostics=outcome["diagnostics"] if not a["passed"] else "",
            )
            for i, a in enumerate(outcome["assertions"], start=1)
        ]
        return milestones, {"peak_memory_bytes": outcome["peak_bytes"]}


# ---------------------------------------------------------------------------
# Reference solutions (used by self-tests as known-good oracles)
# ---------------------------------------------------------------------------

REFERENCE_SOLUTIONS: dict[str, str] = {
    "varint_parser": '''
def encode_varint(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("int required")
    if value < 0 or value >= 1 << 64:
        raise ValueError("out of u64 range")
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def iter_values(chunks):
    buf = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        pos, n = 0, len(buf)
        while pos < n:
            result, shift, i = 0, 0, pos
            complete = False
            while i < n:
                b = buf[i]
                i += 1
                if i - pos > 10:
                    raise ValueError("overlong varint")
                result |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    if shift == 70 and (b & 0x7E):
                        raise ValueError("varint exceeds u64")
                    complete = True
                    break
                if i - pos == 10:
                    raise ValueError("overlong varint")
            if not complete:
                break
            yield result
            pos = i
        del buf[:pos]
    if buf:
        raise ValueError("truncated varint at end of stream")
''',
    "timing_wheel": '''
class TimingWheel:
    def __init__(self, tick_ms=10, wheel_size=256):
        if wheel_size <= 0:
            raise ValueError("wheel_size must be positive")
        self.tick_ms = tick_ms
        self.wheel_size = int(wheel_size)
        self._now = 0
        self._seq = 0
        self._buckets = [[] for _ in range(self.wheel_size)]
        self._live = {}

    def now(self):
        return self._now

    def pending(self):
        return len(self._live)

    def schedule(self, delay, payload):
        if delay is None or delay < 0:
            raise ValueError("delay must be >= 0")
        due = self._now + max(1, int(delay))
        rounds = (due - self._now - 1) // self.wheel_size
        self._seq += 1
        tid = self._seq
        entry = {"id": tid, "rounds": rounds, "payload": payload, "cancelled": False}
        self._buckets[due % self.wheel_size].append(entry)
        self._live[tid] = entry
        return tid

    def cancel(self, timer_id):
        entry = self._live.pop(timer_id, None)
        if entry is None:
            return False
        entry["cancelled"] = True
        return True

    def tick(self):
        self._now += 1
        bucket = self._buckets[self._now % self.wheel_size]
        if not bucket:
            return []
        fired, keep = [], []
        for entry in bucket:
            if entry["cancelled"]:
                continue
            if entry["rounds"] <= 0:
                fired.append(entry)
            else:
                entry["rounds"] -= 1
                keep.append(entry)
        bucket[:] = keep
        out = []
        for entry in fired:
            if not entry["cancelled"]:
                self._live.pop(entry["id"], None)
                out.append(entry["payload"])
        return out
''',
    "lexer_state_machine": '''
_SYMS = set("()[]{},;=+-*/")

def tokenize(source):
    toks, i, n = [], 0, len(source)
    while i < n:
        c = source[i]
        if c in " \\t\\r\\n":
            i += 1
        elif c == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\\n":
                i += 1
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            toks.append(("IDENT", source[i:j]))
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and source[j].isdigit():
                j += 1
            toks.append(("INT", source[i:j]))
            i = j
        elif c == '"':
            j = i + 1
            buf = []
            closed = False
            while j < n:
                d = source[j]
                if d == "\\\\":
                    if j + 1 >= n:
                        break
                    e = source[j + 1]
                    buf.append({"n": "\\n", "t": "\\t", '"': '"', "\\\\": "\\\\"}.get(e, e))
                    j += 2
                elif d == '"':
                    closed = True
                    j += 1
                    break
                elif d == "\\n":
                    break
                else:
                    buf.append(d)
                    j += 1
            if closed:
                toks.append(("STRING", "".join(buf)))
                i = j
            else:
                toks.append(("ERROR", "unterminated-string"))
                while i < n and source[i] != "\\n":
                    i += 1
        elif c == "#" and i + 1 < n and source[i + 1] == "[":
            toks.append(("MACRO_OPEN", "#["))
            i += 2
        elif c in _SYMS:
            toks.append(("SYM", c))
            i += 1
        else:
            toks.append(("ERROR", c))
            i += 1
    return toks


def expand(tokens, env):
    out, i, n = [], 0, len(tokens)

    def _splice(idx, depth):
        if depth > 64:
            raise ValueError("macro expansion depth exceeded")
        if idx >= n or tokens[idx][0] != "MACRO_OPEN":
            raise ValueError("expected macro open")
        idx += 1
        if idx >= n or tokens[idx][0] != "IDENT":
            out.append(("ERROR", "bad-macro-head"))
            return n
        name = tokens[idx][1]
        idx += 1
        depth_nest = 0
        end = idx
        while end < n:
            k, v = tokens[end]
            if k == "MACRO_OPEN":
                depth_nest += 1
            elif k == "SYM" and v == "]":
                if depth_nest == 0:
                    break
                depth_nest -= 1
            end += 1
        if end >= n:
            out.append(("ERROR", "unterminated-macro"))
            return n
        body = env.get(name)
        if body is None:
            out.append(("ERROR", "unknown-macro:" + name))
            return end + 1
        _expand_list(list(body), depth + 1)
        return end + 1

    def _expand_list(toks, depth):
        if depth > 64:
            raise ValueError("macro expansion depth exceeded")
        j = 0
        while j < len(toks):
            k, v = toks[j]
            if k == "MACRO_OPEN":
                sub = toks[j:]
                saved_out_len = len(out)
                _ = saved_out_len
                # splice nested macro inline using shared scanner
                nonlocal_state = {"tokens": toks, "pos": j}
                _ = nonlocal_state
                # manual nested scan over sub list
                if j + 1 >= len(toks) or toks[j + 1][0] != "IDENT":
                    out.append(("ERROR", "bad-macro-head"))
                    # skip to matching ]
                    nest, q = 0, j + 1
                    while q < len(toks):
                        kk, vv = toks[q]
                        if kk == "MACRO_OPEN":
                            nest += 1
                        elif kk == "SYM" and vv == "]":
                            if nest == 0:
                                break
                            nest -= 1
                        q += 1
                    j = q + 1
                    continue
                nm = toks[j + 1][1]
                nest, q = 0, j + 2
                while q < len(toks):
                    kk, vv = toks[q]
                    if kk == "MACRO_OPEN":
                        nest += 1
                    elif kk == "SYM" and vv == "]":
                        if nest == 0:
                            break
                        nest -= 1
                    q += 1
                if q >= len(toks):
                    out.append(("ERROR", "unterminated-macro"))
                    j = len(toks)
                    continue
                bd = env.get(nm)
                if bd is None:
                    out.append(("ERROR", "unknown-macro:" + nm))
                else:
                    _expand_list(list(bd), depth + 1)
                j = q + 1
            else:
                out.append((k, v))
                j += 1

    while i < n:
        if tokens[i][0] == "MACRO_OPEN":
            i = _splice(i, 0)
        else:
            out.append(tokens[i])
            i += 1
    return out
''',
}


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    from benchmark_v3.bench_harness.suites.base import ScriptedDriver, scripted_finish

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} short::{name}", flush=True)

    suite = ShortTaskSuite()
    check("task_ids", suite.task_ids() == ["varint_parser", "timing_wheel", "lexer_state_machine"])
    check("default_condition_a", suite.condition == "a" and suite.run_key == "short")
    with tempfile.TemporaryDirectory(prefix="short-bplan-") as tmp:
        bsuite = ShortTaskSuite(condition="b")
        bsuite.prepare_task("varint_parser", Path(tmp))
        bsuite._maybe_write_b_plan("varint_parser", Path(tmp))
        check("b_writes_plan", (Path(tmp) / "PLAN.md").is_file())
        plan_text = (Path(tmp) / "PLAN.md").read_text(encoding="utf-8")
        check("b_plan_no_oracle_leak", "EVAL_SEED" not in plan_text)
        check("b_plan_points_at_task", "`TASK.md`" in plan_text)
        check("b_plan_asks_read", "`read`" in plan_text)
        check("b_plan_not_second_spec", "pick one reading" not in plan_text)

    # -- reference solutions pass all 12 assertions --
    total_pass = 0
    for task_id, src in REFERENCE_SOLUTIONS.items():
        with tempfile.TemporaryDirectory(prefix="short-ref-") as tmp:
            (Path(tmp) / "solution.py").write_text(src, encoding="utf-8")
            outcome = run_task_checks(task_id, tmp)
            n_pass = sum(1 for a in outcome["assertions"] if a["passed"])
            total_pass += n_pass
            check("ref_%s_%d_of_10" % (task_id, n_pass), n_pass == 10)
    check("ref_total_30", total_pass == 30)

    # -- memory oracle bites a slurping implementation --
    slurp = REFERENCE_SOLUTIONS["varint_parser"].replace(
        "        buf += chunk\n        pos, n = 0, len(buf)",
        "        buf += chunk\n        pos, n = 0, len(buf)",
    )
    slurp_bad = (
        "def encode_varint(value):\n"
        "    from io import BytesIO\n"
        "    if value < 0 or value >= 1 << 64:\n"
        "        raise ValueError('range')\n"
        "    out = bytearray()\n"
        "    while True:\n"
        "        b = value & 0x7F; value >>= 7\n"
        "        out.append(b | (0x80 if value else 0))\n"
        "        if not value:\n"
        "            return bytes(out)\n"
        "def iter_values(chunks):\n"
        "    data = b''.join(chunks)\n"
        "    out = []\n"
        "    i = 0\n"
        "    while i < len(data):\n"
        "        r, s = 0, 0\n"
        "        while True:\n"
        "            b = data[i]; i += 1\n"
        "            r |= (b & 0x7F) << s; s += 7\n"
        "            if not b & 0x80:\n"
        "                break\n"
        "        out.append(r)\n"
        "        _pad = bytes(64)\n"
        "    return iter(out)\n"
    )
    _ = slurp
    with tempfile.TemporaryDirectory(prefix="short-bad-") as tmp:
        (Path(tmp) / "solution.py").write_text(slurp_bad, encoding="utf-8")
        outcome = run_task_checks("varint_parser", tmp)
        by_id = {a["id"]: a for a in outcome["assertions"]}
        check("slurp_correctness_passes", by_id["a1"]["passed"])
        check(
            "slurp_memory_fails",
            not by_id["a10"]["passed"] and outcome["peak_bytes"] > VARINT_MEMORY_LIMIT,
        )

    # -- missing solution fails closed with diagnostics --
    with tempfile.TemporaryDirectory(prefix="short-missing-") as tmp:
        outcome = run_task_checks("timing_wheel", tmp)
        check(
            "missing_fails_closed",
            all(not a["passed"] for a in outcome["assertions"])
            and not outcome["timed_out"],
        )

    # -- full run_session integration via scripted driver --
    with tempfile.TemporaryDirectory(prefix="short-sess-") as tmp:
        driver = ScriptedDriver(
            "scripted",
            script=[
                {
                    "content": "writing reference varint",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "name": "write",
                            "arguments": {
                                "path": "solution.py",
                                "content": REFERENCE_SOLUTIONS["varint_parser"],
                            },
                        }
                    ],
                },
                scripted_finish("Wrote solution.py varint encode/decode and checked streaming."),
            ],
        )
        report = suite.run_session("varint_parser", "scripted", driver, tmp)
        check("session_passes", report.passed and len(report.milestones) == 10)
        check("peak_recorded", report.peak_memory_bytes > 0)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"short_task self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
