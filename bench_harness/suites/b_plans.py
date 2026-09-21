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
1. `read` `TASK.md`. Constructor parameters (`tick_ms=10`, `wheel_size=256`),
   method signatures (`schedule`, `cancel`, `tick`, `now`, `pending`),
   time unit semantics, and amortised O(1) performance limits are binding.
2. Implement `TimingWheel` in `solution.py`:
   - manage internal tick counter `_now` advancing by 1 per `tick()`;
   - use circular buckets list of size `wheel_size` plus per-timer tracking;
   - `schedule(delay, payload)`: calculate target tick `now + max(1, delay)`;
     return unique int timer id;
   - `cancel(timer_id)`: remove from pending; return `True` if active, `False`
     if unknown or already fired;
   - `tick()`: advance `_now`, collect and remove due timers in FIFO insertion
     order, return due payloads list;
   - `now()` and `pending()` return current tick and live timer count.
3. Deliverable is ONLY `solution.py`. Stdlib only.

## Self-check (run via `bash` heredoc / `python -c`; no scratch files)
S1. Instantiate with default and custom parameters; verify `now()==0` and
    advances by 1 on each `tick()`.
S2. Schedule delay 0 and delay 1: confirm both fire on the very next tick;
    verify multiple payloads at the same tick fire in exact FIFO order.
S3. Schedule future timers: cancel a pending timer (verify returns `True` and
    payload never fires); cancel an unknown ID or already-fired ID (verify
    returns `False`).
S4. Check `pending()` count dynamically increases on schedule, decreases on
    cancel, and decreases when timers fire.
S5. Benchmark test: schedule tens of thousands of random-delay timers across
    thousands of ticks; verify completion in under a few seconds with zero
    leaked pending timers, then `finish`.
""",
    "lexer_state_machine": _HEADER + """
## Order
1. `read` `TASK.md`. Token categories (`IDENT`, `INT`, `STRING`, `SYM`,
   `MACRO_OPEN`, `ERROR`), allowed symbols `()[]{},;=+-*/`, escape rules,
   error-recovery semantics, and `expand` recursive splicing are binding.
2. Implement `tokenize(source: str)` in `solution.py`:
   - skip whitespace and `//` line comments up to newline;
   - scan identifiers/keywords `[a-zA-Z_][a-zA-Z0-9_]*` and integers `[0-9]+`;
   - scan strings `"..."` with escapes `\\\\`, `\\"`, `\\n`, `\\t`; unclosed
     strings emit `('ERROR', ...)` and resume scanning on next line without raising;
   - `#[` emits `('MACRO_OPEN', '#[')`; any `#` not followed by `[` emits `ERROR`;
   - single-character symbols in `()[]{},;=+-*/` emit `('SYM', ch)`; any other
     unrecognized character emits `('ERROR', ch)`.
3. Implement `expand(tokens, env)` in `solution.py`:
   - depth-first recursive macro replacement matching `#[NAME]` up to matching `]`;
   - unknown macro names or unclosed macro constructs emit `ERROR` tokens;
   - recursion depth > 64 or cyclic references must raise `ValueError`.
4. Deliverable is ONLY `solution.py`. Stdlib only.

## Self-check (run via `bash` heredoc / `python -c`; no scratch files)
S1. Tokenize sample expressions: verify identifiers, integers, symbols, and
    multiline comments.
S2. Tokenize string with escape sequences (`\\\\`, `\\"`, `\\n`, `\\t`); verify
    unescaped string content matches expectations.
S3. Error recovery: test stray characters (`@`, unattached `#`), unclosed
    string; verify `tokenize` returns `ERROR` tokens and never raises exceptions.
S4. Macro expansion: test basic `#[NAME]` replacement and nested macros (inner
    macros expanding inside outer macro bodies).
S5. Robustness: test unknown macro name and unclosed macro syntax (both emit
    `ERROR` tokens); test cyclic macro dependency (must raise `ValueError`),
    then `finish`.
""",
    "raft_cluster": _HEADER + """
## Order
1. `read` `TASK.md`. The process command line form, mailbox envelope shape,
   message field names, partitions file, durability rules, the election
   marker line, and the liveness timing ranges there are binding. Do not
   invent extra protocol rules.
2. Build the mailbox layer first, in `raft.py` (stdlib only):
   - write outgoing messages to `<broker_root>/mailbox_<dst>/msg-*.json`
     atomically (temp file + `os.replace`), never partially visible;
   - poll your own mailbox, skip files that fail to parse, ignore messages
     whose `dst` is not you, honour the partition matrix on every send and
     receive, and delete each file once consumed.
3. Add persistent state (`state.json` in your workdir): term, voted_for,
   log, commit_index, store, results. Persist atomically before every
   client-visible acknowledgement.
4. Implement the roles: follower → candidate → leader with the randomised
   election timeout stated in `TASK.md`; RequestVote/Vote handling must
   respect log up-to-dateness; heartbeats on the interval stated there.
5. Implement replication and commit: track per-peer match progress, append
   entries, and only advance commit_index by the majority rule for entries
   of the current term; apply committed entries to the in-memory store and
   record per-request results.
6. Implement the client surface: `client_write`, `client_read`,
   `client_status`, `bye`. Every reply echoes the request `req_id`; a write
   is acked only after majority commit; a minority-side write must time out
   unacked rather than commit.
7. Observability: append the leadership observation line to
   `elections_<id>.log` whenever a new term's leader is known, and print the
   flush-required marker exactly as `TASK.md` specifies immediately before
   persisting a client-bearing commit.
8. Deliverable is ONLY `raft.py`. Stdlib only.

## Self-check (drive the real protocol yourself via `bash`; do NOT leave
## scratch files in the workspace — only `raft.py` is scored)
S1. Start three nodes against a scratch broker root you create as a
    workspace-relative subdirectory (absolute paths such as `/tmp/...` are
    refused by the harness sandbox), then query status until a leader is
    reported and the election log gains its observation line. Confirm no
    node answers for another node's mailbox.
S2. Through the reported leader, perform at least 20 sequential writes and
    read them back; confirm every acked write survives a restart of one
    follower (kill its process, relaunch with the same workdir, re-read).
S3. Split the cluster with a partitions cut that isolates exactly the
    current leader. Confirm the remaining majority elects a new leader and
    keeps acking writes, and that a write attempted on the isolated side is
    never acked. Heal the cut and confirm the lagging node converges to the
    same store as the majority.
S4. Terminate one node abruptly (not a clean shutdown), restart it from its
    workdir, and confirm it rejoins, its term/log/commit_index were
    durable, and no acked write was lost.
S5. Re-read `TASK.md`: the exact command line, envelope fields, timing
    ranges, marker line, and durability guarantees all hold; no extra
    rejection rule was invented; then `finish`.
""",
    "saga_coordinator": _HEADER + """
## Order
1. `read` `TASK.md`. The `Coordinator` constructor signature, op shape, the
   service request/ack protocol, the WAL event vocabulary, the marker line,
   the recovery contract, and the deadlock helpers there are binding.
2. Build the messaging layer in `saga.py` (stdlib only): send to and receive
   from service mailboxes with the same envelope shape the services use,
   match replies strictly by `req_id`, and honour the partitions cut file on
   every send and receive.
3. Implement the transactional API in the order the contract lists them:
   `begin` allocates a txid and appends the begin record; `prepare` sends
   prepares in parallel and collects per-service verdicts; `prepare_more`
   extends an existing transaction; `commit` performs the two-phase commit
   with the retry budget `TASK.md` states; `rollback` reverses participants
   that may have prepared; `execute` composes begin → prepare → commit with
   rollback on any failure.
4. Durability: append the WAL records exactly as `TASK.md` enumerates, with
   fsync on append; before persisting a client-bearing commit print the
   flush-required marker. `recover()` must scan the WAL, resolve every
   dangling prepare (commit only when all participants still hold their
   prepared/committed side, otherwise abort) and be idempotent — a second
   call performs no further work.
5. Deadlocks: implement the static cycle helper over a waits graph and the
   runtime resolver that queries participants' `waits`, finds an actual
   cycle, and aborts the youngest member so the rest can proceed.
6. Timeouts everywhere: prepare and commit waits bounded by the constructor
   timeout, the commit retry count bounded as specified, and an isolated
   participant must cause an abort — never an indefinite hang.
7. Deliverable is ONLY `saga.py`. Stdlib only.

## Self-check (run via `bash` heredoc / `python -c`; do NOT leave scratch
## files in the workspace — only `saga.py` is scored)
S1. Happy path: run a single-service op and a multi-service op through
    `execute`; confirm every involved participant ends committed and the
    WAL contains the full begin → prepared → commit-start → committed
    sequence for each txid.
S2. Failure path: make one participant refuse a prepare (wrong item or an
    unavailable service), then confirm `execute` returns failure, every
    other participant is rolled back, and no partial commit is visible.
S3. Crash recovery: start a multi-service commit and terminate the
    coordinator between its commit-start record and its committed record;
    relaunch, call `recover()`, and confirm the dangling transaction is
    resolved exactly once; a second `recover()` returns nothing to do.
S4. Isolation: cut one participant from the coordinator, run an op, and
    confirm it aborts within the timeout instead of hanging; heal the cut
    and confirm a subsequent op commits successfully.
S5. Deadlock: build two transactions that hold each other's items, confirm
    the cycle helper reports an actual cycle spanning multiple txids and the
    resolver aborts exactly the youngest member while the other completes;
    re-read `TASK.md` for the timeout, retry, and recovery rules, then
    `finish`.
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

    #: 红线：断言 id / 评估种子 / 参考实现内部常量 一律不得出现在计划里
    forbidden = (
        "EVAL_SEED",
        "pick one reading",
        "overlong (>10",
        "hashed wheel",
        "2654435761",
        "SIGKILL",
    )
    #: 计划必须给出可核验的具体动作（Order 步骤 + Self-check 步骤）
    for task_id, text in B_PLANS.items():
        check(f"{task_id}_points_at_task", "`TASK.md`" in text)
        check(f"{task_id}_asks_read", "`read`" in text)
        check(f"{task_id}_has_order", "## Order" in text)
        check(f"{task_id}_has_selfcheck", "## Self-check" in text)
        check(f"{task_id}_has_s1", "S1." in text)
        for needle in forbidden:
            check(f"{task_id}_no_{needle!r}", needle not in text)
    fallback = plan_for("nope")
    check("unknown_fallback_task", "`TASK.md`" in fallback)
    return counts[0], counts[1]


if __name__ == "__main__":
    passed, failed = self_test()
    raise SystemExit(0 if failed == 0 else 1)
