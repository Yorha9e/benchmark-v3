"""Condition-B frozen plans (work order only).

Injected as ``PLAN.md`` plus a short prompt appendix for ``short_b`` /
``long_b``. Hard constraints live in ``TASK.md``; these texts are a
suggested order of work. No reference code, no hidden assertion names,
no seeds, no paraphrased spec.
"""

from __future__ import annotations

_HEADER = (
    "# Implementation plan\n"
    "\n"
    "Hard constraints are **only** in `TASK.md`. Use the `read` tool on\n"
    "`TASK.md` before you edit anything. This file is a suggested order of\n"
    "work, not a specification. If this plan and `TASK.md` disagree, follow\n"
    "`TASK.md` and ignore the plan.\n"
)

B_PLANS: dict[str, str] = {
    "varint_parser": _HEADER + """
## Order
1. `read` `TASK.md`. Treat every public API, error, and resource limit it
   states as binding. Do not invent extra rules.
2. Implement `encode_varint` in `solution.py`: base-128 little-endian, 7
   payload bits per byte with the high bit as continuation; values 0..127
   are exactly one byte; raise `ValueError` for inputs outside u64 range.
3. Implement `iter_values` in `solution.py` as a lazy generator (yield one
   value at a time, never accumulate the whole stream):
   - keep a small byte buffer across chunks; skip empty chunks;
   - consume input strictly left to right, drop consumed prefixes so the
     buffer never grows with stream length;
   - a stream ending mid-varint raises `ValueError`;
   - more than 10 bytes with the continuation bit set raises `ValueError`;
   - on the 10th byte only the lowest payload bit may be set (u64 range),
     otherwise raise `ValueError`.
4. On-disk deliverable is ONLY `solution.py`. Stdlib only.

## Self-check (run via `bash` heredoc / `python -c`; do NOT leave scratch
## files in the workspace — only `solution.py` is scored)
S1. Roundtrip 0..127 one byte each; boundary values 128, 300, 16384,
    2**32-1, 2**63-1, 2**64-1 decode back exactly.
S2. One fixed byte string decoded under random chunk splits — including
    every-byte-alone splits and empty chunks mixed in — returns the same
    values every time.
S3. A cut-off tail (last byte removed) raises `ValueError`; 11
    continuation bytes in a row raise `ValueError`; `encode_varint(-1)`
    and `encode_varint(2**64)` raise.
S4. Feed a multi-MiB byte stream through a generator (never `list()` the
    input or output) under `tracemalloc` and confirm the peak stays
    clearly below the `TASK.md` memory limit.
S5. Re-read `TASK.md`: every listed behaviour holds, no extra rejection
    rule was invented, then `finish`.
""",
    "timing_wheel": _HEADER + """
## Order
1. `read` `TASK.md`. The class name, methods, time model, and performance
   limit there are binding.
2. Implement that class in `solution.py`.
3. Implement the methods `TASK.md` lists (schedule, cancel, tick, now,
   pending) so they match the contract in `TASK.md`, not this outline.
4. Edit only the files `TASK.md` allows. Stdlib only.

## Self-check
Re-read `TASK.md` and confirm delays, cancel, FIFO, and the performance
limit it states all hold. Do not treat this plan as a second spec.
""",
    "lexer_state_machine": _HEADER + """
## Order
1. `read` `TASK.md`. Token kinds, escapes, error recovery, and expand
   rules there are binding. Do not guess character classes or string
   values beyond what `TASK.md` states.
2. Implement `tokenize` in `solution.py` exactly as `TASK.md` specifies.
3. Implement `expand` as `TASK.md` specifies (including depth / cycle
   failure).
4. Edit only the files `TASK.md` allows. Stdlib only.

## Self-check
Re-read `TASK.md`. If a token or expand rule is not written there, do
not add it. Do not treat this plan as a second spec.
""",
    "raft_cluster": _HEADER + """
## Order
1. `read` `TASK.md`. The process command line, wire format, roles,
   partitions file, marker line, and durability rules there are binding.
2. Create only `raft.py` (stdlib only), as `TASK.md` requires.
3. Suggested build order: mailbox I/O → persistent state → follower /
   candidate / leader roles → client writes. Each step must still match
   `TASK.md`, not this list.
4. Honour every line of `TASK.md`, including cuts and restart.

## Self-check
Re-read `TASK.md`. A connected majority, client durability, and split
safety are whatever that document specifies. Do not treat this plan as
a second spec.
""",
    "saga_coordinator": _HEADER + """
## Order
1. `read` `TASK.md`. Constructor, mailbox, transaction API, WAL, marker
   line, timeouts, retries, and deadlock helpers there are binding.
2. Implement `Coordinator` in `saga.py` only (stdlib only).
3. Suggested build order: mailbox + cuts → begin/prepare/commit/rollback
   /execute → WAL + recover → deadlock helpers. Each step must still
   match `TASK.md`.
4. Never hang on an isolated participant; abort as `TASK.md` requires.

## Self-check
Re-read `TASK.md`. Happy-path atomicity and recovery are whatever that
document specifies. Do not treat this plan as a second spec.
""",
}


def plan_for(task_id: str) -> str:
    text = B_PLANS.get(task_id)
    if text:
        return text
    return (
        _HEADER
        + "\n## Order\n1. `read` `TASK.md`.\n"
        + "2. Implement only what that file specifies.\n"
    )


def self_test() -> tuple[int, int]:
    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} b_plans::{name}", flush=True)

    forbidden = (
        "EVAL_SEED",
        "pick one reading",
        "overlong (>10",
        "hashed wheel",
    )
    for task_id, text in B_PLANS.items():
        check(f"{task_id}_points_at_task", "`TASK.md`" in text)
        check(f"{task_id}_asks_read", "`read`" in text)
        for needle in forbidden:
            check(f"{task_id}_no_{needle!r}", needle not in text)
    fallback = plan_for("nope")
    check("unknown_fallback_task", "`TASK.md`" in fallback)
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
