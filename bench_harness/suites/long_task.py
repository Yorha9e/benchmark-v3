"""Next-gen long-task suite: distributed fault-tolerant micro-services.

Two tasks, each scored by 10 milestones x 2 sub-assertions (40 assertions
across the suite), driven through the full Jepsen stack:

1. ``raft_cluster`` — the model ships ``raft.py`` (a 3-node Raft node
   honouring the harness wire/file contract). The harness spawns three
   real node processes (:class:`SupervisorManager`), injects a Jepsen
   split-brain partition plus a marker-triggered external SIGKILL
   (:class:`MarkerWatcher` + ``hard_kill_pid``), heals, and validates with
   :class:`InvariantChecker` (single-leader, commit persistence, final
   consistency, linearizability).
2. ``saga_coordinator`` — the model ships ``saga.py`` (a ``Coordinator``
   over three SQLite-backed services). Services run as parent-side
   threads (so they survive the coordinator SIGKILL), the coordinator
   runs as a supervised child process killed at ``CRITICAL_WRITE_POINT``,
   and recovery/deadlock/partition behaviour is validated, including a
   torn-write (``corrupt_file``) fail-closed check.

Network faults are enforced through ``<broker>/partitions.json`` (read by
every participant on each send/recv); :class:`ChaosNemesis` drives the
fault timelines with ``PartitionMatrix`` bookkeeping, so all four Jepsen
components (supervisor, broker/matrix, nemesis, checker) are genuinely
integrated. Wall-clock time is pure telemetry throughout.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.runner import ProcessRunner
from benchmark_v3.bench_harness.core.types import AgentTrajectory, MilestoneResult
from benchmark_v3.bench_harness.jepsen.broker import FileMessageBroker
from benchmark_v3.bench_harness.jepsen.checker import InvariantChecker
from benchmark_v3.bench_harness.jepsen.nemesis import (
    ChaosNemesis,
    ScenarioTimeline,
)
from benchmark_v3.bench_harness.jepsen.supervisor import (
    SupervisorManager,
)
from benchmark_v3.bench_harness.suites.base import SuiteAdapter, mk_milestone

__all__ = [
    "LongTaskSuite",
    "RAFT_NODE_IDS",
    "SAGA_SERVICES",
    "read_partitions",
    "run_raft_scenario",
    "run_saga_scenario",
    "write_partitions",
]

RAFT_NODE_IDS = ("n1", "n2", "n3")
BUS_IDS = ("n1", "n2", "n3", "client")
SAGA_SERVICES = ("inventory", "payment", "shipping")
COORD_ID = "coord"

CRITICAL_MARKER = "CRITICAL_WRITE_POINT"


# ---------------------------------------------------------------------------
# partitions.json helpers (the enforced fault plane)
# ---------------------------------------------------------------------------

def write_partitions(broker_root: str | Path, cuts: list[tuple[str, str]]) -> None:
    """Atomically publish bidirectional cuts ``[(a, b), ...]``.

    Node/coordinator children poll this file constantly, so on Windows
    ``os.replace`` can transiently fail (WinError 5/32) while a reader holds
    a handle. Retry briefly instead of crashing the scenario.
    """
    path = Path(broker_root) / "partitions.json"
    tmp = path.with_name("partitions.json.tmp-%d" % os.getpid())
    tmp.write_text(json.dumps({"cuts": [list(c) for c in cuts]}), encoding="utf-8")
    last_exc: OSError | None = None
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            last_exc = None
            break
        except OSError as exc:
            last_exc = exc
            if attempt == 9:
                break
            time.sleep(0.02 * (attempt + 1))
    if last_exc is not None:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise last_exc


def read_partitions(broker_root: str | Path) -> set[tuple[str, str]]:
    try:
        data = json.loads((Path(broker_root) / "partitions.json").read_text(encoding="utf-8"))
        return {(str(a), str(b)) for a, b in data.get("cuts", [])}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def _cut_blocks(cuts: set[tuple[str, str]], a: str, b: str) -> bool:
    return (a, b) in cuts or (b, a) in cuts


# ---------------------------------------------------------------------------
# Reference raft.py (node implementation honouring the harness contract)
# ---------------------------------------------------------------------------

REFERENCE_RAFT = '''"""Raft cluster node (reference implementation, stdlib only).

Harness contract:
  argv: raft.py <node_id> <broker_root> <workdir> <seed>
  mailbox layout: <broker_root>/mailbox_<id>/msg-*.json, envelope
    {seq, src, dst, send_time, deliver_at, payload}
  <broker_root>/partitions.json: {"cuts": [[a, b], ...]} (bidirectional)
  files in workdir: state.json, elections_<id>.log (stdout = node log)
Messages: RequestVote/Vote/Append/Ack/client_write/client_read/client_status.
"""

import json
import os
import random
import sys
import time

HEARTBEAT = 0.05
POLL = 0.01


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json_atomic(path, obj):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.write("\\n")
    os.replace(tmp, path)


def _mbox_send(root, src, dst, payload):
    box = os.path.join(root, "mailbox_" + dst)
    try:
        os.makedirs(box, exist_ok=True)
    except OSError:
        pass
    env = {"seq": random.getrandbits(62), "src": src, "dst": dst,
           "send_time": time.time(), "deliver_at": time.time(), "payload": payload}
    # Underscore name: matches the harness broker's msg_*.json mailbox glob.
    name = "msg_%024x.json" % random.getrandbits(96)
    tmp = os.path.join(box, ".tmp-%d.json" % random.getrandbits(32))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        os.replace(tmp, os.path.join(box, name))
    except OSError:
        pass


def _mbox_recv(root, me):
    box = os.path.join(root, "mailbox_" + me)
    try:
        names = sorted(os.listdir(box))
    except OSError:
        return []
    out, now = [], time.time()
    for name in names:
        if (not name.startswith("msg-") and not name.startswith("msg_")) or not name.endswith(".json"):
            continue
        path = os.path.join(box, name)
        try:
            with open(path, encoding="utf-8") as fh:
                env = json.load(fh)
            if float(env.get("deliver_at", 0)) > now:
                continue
            out.append((env.get("src"), env.get("payload")))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            os.remove(path)
        except OSError:
            pass
    return out


class _Cuts:
    def __init__(self, root):
        self.root = root
        self.cuts = set()
        self.ts = 0.0

    def blocked(self, a, b):
        now = time.monotonic()
        if now - self.ts > 0.05:
            data = _read_json(os.path.join(self.root, "partitions.json"), {})
            raw = data.get("cuts", []) if isinstance(data, dict) else []
            self.cuts = {(str(x), str(y)) for x, y in raw}
            self.ts = now
        return (a, b) in self.cuts or (b, a) in self.cuts


class RaftNode:
    def __init__(self, node_id, broker_root, workdir, seed):
        self.id = node_id
        self.peers = ["n1", "n2", "n3"]
        self.root = broker_root
        self.workdir = workdir
        self.rng = random.Random(seed + sum(ord(c) for c in node_id))
        self.cuts = _Cuts(broker_root)
        os.makedirs(workdir, exist_ok=True)
        st = _read_json(os.path.join(workdir, "state.json"), {})
        self.term = int(st.get("term", 0))
        self.voted_for = st.get("voted_for")
        self.log = list(st.get("log", []))
        self.commit_index = int(st.get("commit_index", 0))
        self.store = dict(st.get("store", {}))
        self.results = dict(st.get("results", {}))
        self.role = "follower"
        self.leader_id = None
        self.votes = set()
        self.next_idx = {}
        self.match_idx = {}
        self.election_deadline = time.monotonic() + self._timeout()
        self.next_heartbeat = 0.0
        self.last_logged = None
        self.elect_log = os.path.join(workdir, "elections_%s.log" % node_id)

    def _timeout(self):
        return 0.18 + self.rng.random() * 0.15

    def persist(self):
        _write_json_atomic(os.path.join(self.workdir, "state.json"), {
            "term": self.term, "voted_for": self.voted_for, "log": self.log,
            "commit_index": self.commit_index, "store": self.store,
            "results": self.results})

    def log_election(self, term, leader):
        if self.last_logged == (term, leader):
            return
        self.last_logged = (term, leader)
        try:
            with open(self.elect_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"term": term, "leader": leader}) + "\\n")
        except OSError:
            pass

    def send(self, dst, msg):
        if self.cuts.blocked(self.id, dst):
            return
        _mbox_send(self.root, self.id, dst, msg)

    def last_info(self):
        if self.log:
            return len(self.log), self.log[-1]["term"]
        return 0, 0

    def apply_entries(self, entries):
        advanced = []
        for entry in entries:
            self.store[entry["key"]] = entry["value"]
            if entry.get("req_id"):
                self.results[entry["req_id"]] = {"ok": True}
                advanced.append(entry["req_id"])
        return advanced

    def step_down(self, term):
        if term > self.term:
            self.term = term
            self.voted_for = None
            self.persist()
        if self.role != "follower":
            self.role = "follower"
            self.leader_id = None

    # -- message handlers -------------------------------------------------
    def on_vote_request(self, src, m):
        term = int(m.get("term", 0))
        if term < self.term:
            self.send(src, {"type": "Vote", "term": self.term, "granted": False})
            return
        self.step_down(term)
        last_idx, last_term = self.last_info()
        up_to_date = (int(m.get("last_term", 0)), int(m.get("last_idx", 0))) >= (last_term, last_idx)
        if (self.voted_for in (None, m.get("candidate"))) and up_to_date:
            self.voted_for = m.get("candidate")
            self.persist()
            self.election_deadline = time.monotonic() + self._timeout()
            self.send(src, {"type": "Vote", "term": self.term, "granted": True})
        else:
            self.send(src, {"type": "Vote", "term": self.term, "granted": False})

    def on_vote(self, src, m):
        term = int(m.get("term", 0))
        if term > self.term:
            self.step_down(term)
            self.election_deadline = time.monotonic() + self._timeout()
            return
        if self.role == "candidate" and term == self.term and m.get("granted"):
            self.votes.add(src)
            if len(self.votes) >= 2:
                self.role = "leader"
                self.leader_id = self.id
                last = len(self.log)
                for peer in self.peers:
                    if peer != self.id:
                        self.next_idx[peer] = last + 1
                        self.match_idx[peer] = 0
                self.log_election(self.term, self.id)
                self.persist()
                self.send_heartbeats()

    def on_append(self, src, m):
        term = int(m.get("term", 0))
        if term < self.term:
            self.send(src, {"type": "Ack", "term": self.term, "ok": False})
            return
        self.step_down(term)
        self.leader_id = m.get("leader")
        self.log_election(self.term, self.leader_id)
        self.election_deadline = time.monotonic() + self._timeout()
        prev_idx = int(m.get("prev_idx", 0))
        prev_term = int(m.get("prev_term", 0))
        if prev_idx > 0 and (len(self.log) < prev_idx or self.log[prev_idx - 1]["term"] != prev_term):
            self.send(src, {"type": "Ack", "term": self.term, "ok": False})
            return
        entries = m.get("entries", []) or []
        if entries:
            self.log = self.log[:prev_idx] + entries
            self.persist()
        leader_commit = int(m.get("leader_commit", 0))
        if leader_commit > self.commit_index:
            new_commit = min(leader_commit, len(self.log))
            self.apply_entries(self.log[self.commit_index:new_commit])
            self.commit_index = new_commit
            self.persist()
        self.send(src, {"type": "Ack", "term": self.term, "ok": True,
                        "match_idx": len(self.log)})

    def on_ack(self, src, m):
        term = int(m.get("term", 0))
        if term > self.term:
            self.step_down(term)
            self.election_deadline = time.monotonic() + self._timeout()
            return
        if self.role != "leader" or term != self.term:
            return
        if m.get("ok"):
            self.match_idx[src] = max(self.match_idx.get(src, 0), int(m.get("match_idx", 0)))
            self.next_idx[src] = self.match_idx[src] + 1
            self.advance_commit()
        else:
            self.next_idx[src] = max(1, self.next_idx.get(src, 1) - 1)
            self.send_one(src)

    def advance_commit(self):
        for n in range(len(self.log), self.commit_index, -1):
            if self.log[n - 1]["term"] != self.term:
                continue
            count = 1 + sum(1 for p in self.peers
                            if p != self.id and self.match_idx.get(p, 0) >= n)
            if count >= 2:
                newly = self.log[self.commit_index:n]
                self.commit_index = n
                reqs = self.apply_entries(newly)
                if reqs:
                    print("CRITICAL_WRITE_POINT", flush=True)
                self.persist()
                for req_id in reqs:
                    self.send("client", {"type": "write_ack", "req_id": req_id, "ok": True})
                break

    def on_client_write(self, m):
        req_id = m.get("req_id")
        if self.role != "leader":
            self.send("client", {"type": "write_ack", "req_id": req_id,
                                 "ok": False, "leader": self.leader_id})
            return
        if req_id in self.results:
            self.send("client", {"type": "write_ack", "req_id": req_id, "ok": True})
            return
        self.log.append({"term": self.term, "key": m.get("key"),
                         "value": m.get("value"), "req_id": req_id})
        self.persist()
        for peer in self.peers:
            if peer != self.id:
                self.send_one(peer)

    # -- timers ------------------------------------------------------------
    def send_one(self, peer):
        nxt = self.next_idx.get(peer, 1)
        prev_idx = nxt - 1
        prev_term = self.log[prev_idx - 1]["term"] if prev_idx > 0 else 0
        self.send(peer, {"type": "Append", "term": self.term, "leader": self.id,
                         "prev_idx": prev_idx, "prev_term": prev_term,
                         "entries": self.log[prev_idx:],
                         "leader_commit": self.commit_index})

    def send_heartbeats(self):
        for peer in self.peers:
            if peer != self.id:
                self.send_one(peer)
        self.next_heartbeat = time.monotonic() + HEARTBEAT

    def campaign(self):
        self.term += 1
        self.role = "candidate"
        self.voted_for = self.id
        self.votes = {self.id}
        self.leader_id = None
        self.persist()
        self.election_deadline = time.monotonic() + self._timeout()
        last_idx, last_term = self.last_info()
        for peer in self.peers:
            if peer != self.id:
                self.send(peer, {"type": "RequestVote", "term": self.term,
                                 "candidate": self.id, "last_idx": last_idx,
                                 "last_term": last_term})

    def step(self):
        for src, msg in _mbox_recv(self.root, self.id):
            if not isinstance(msg, dict):
                continue
            if self.cuts.blocked(src, self.id):
                continue
            kind = msg.get("type")
            try:
                if kind == "RequestVote":
                    self.on_vote_request(src, msg)
                elif kind == "Vote":
                    self.on_vote(src, msg)
                elif kind == "Append":
                    self.on_append(src, msg)
                elif kind == "Ack":
                    self.on_ack(src, msg)
                elif kind == "client_write":
                    self.on_client_write(msg)
                elif kind == "client_read":
                    if self.role == "leader":
                        self.send("client", {"type": "read_ack", "req_id": msg.get("req_id"),
                                             "value": self.store.get(msg.get("key"))})
                    else:
                        self.send("client", {"type": "read_ack", "req_id": msg.get("req_id"),
                                             "value": None, "leader": self.leader_id})
                elif kind == "client_status":
                    self.send("client", {"type": "status_ack", "req_id": msg.get("req_id"),
                                         "node": self.id,
                                         "role": self.role, "term": self.term,
                                         "leader": self.leader_id})
                elif kind == "bye":
                    sys.exit(0)
            except SystemExit:
                raise
            except Exception as exc:
                sys.stderr.write("node %s handler error: %r\\n" % (self.id, exc))
        now = time.monotonic()
        if self.role == "leader":
            if now >= self.next_heartbeat:
                self.send_heartbeats()
        elif now >= self.election_deadline:
            time.sleep(self.rng.random() * 0.01)
            self.campaign()


def main(argv):
    node_id, broker_root, workdir = argv[1], argv[2], argv[3]
    seed = int(argv[4]) if len(argv) > 4 else 0
    node = RaftNode(node_id, broker_root, workdir, seed)
    while True:
        try:
            node.step()
        except SystemExit:
            raise
        except Exception as exc:
            sys.stderr.write("node %s loop error: %r\\n" % (node_id, exc))
        time.sleep(POLL)


if __name__ == "__main__":
    main(sys.argv)
'''


# ---------------------------------------------------------------------------
# Raft scenario driver (parent side)
# ---------------------------------------------------------------------------

class _RaftClient:
    """Parent-side client speaking the node wire protocol via FileMessageBroker."""

    def __init__(self, broker: FileMessageBroker) -> None:
        self.broker = broker
        self._seq = 0

    def _req_id(self, prefix: str) -> str:
        self._seq += 1
        return "%s-%d-%s" % (prefix, self._seq, uuid.uuid4().hex[:6])

    def _roundtrip(self, target: str, payload: dict[str, Any], timeout: float) -> dict | None:
        self.broker.send("client", target, payload)
        deadline = time.monotonic() + timeout
        want = payload.get("req_id")
        while time.monotonic() < deadline:
            for _, msg in self.broker.recv("client"):
                if isinstance(msg, dict) and msg.get("req_id") == want:
                    return msg
            time.sleep(0.02)
        return None

    def status(self, node: str, timeout: float = 2.0) -> dict | None:
        return self._roundtrip(node, {"type": "client_status", "req_id": self._req_id("st")}, timeout)

    def write(self, node: str, key: str, value: Any, timeout: float = 4.0) -> tuple[bool, str]:
        req_id = self._req_id("w")
        start = time.monotonic()
        reply = self._roundtrip(node, {"type": "client_write", "req_id": req_id,
                                       "key": key, "value": value}, timeout)
        end = time.monotonic()
        ok = bool(reply and reply.get("ok"))
        return ok, req_id, start, end

    def read(self, node: str, key: str, timeout: float = 3.0) -> tuple[Any, bool]:
        reply = self._roundtrip(node, {"type": "client_read", "req_id": self._req_id("r"),
                                       "key": key}, timeout)
        if not reply:
            return None, False
        return reply.get("value"), True


def _wait_leader(client: _RaftClient, timeout: float) -> tuple[str | None, int]:
    """Return the highest-term leadership claimant (stable across a re-poll).

    Preferring the max term keeps a stale isolated leader (old term) from
    shadowing the real majority leader elected during a partition.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        claimants: dict[str, int] = {}
        for node in RAFT_NODE_IDS:
            try:
                st = client.status(node, timeout=0.6)
            except Exception:
                st = None
            if st and st.get("role") == "leader":
                claimants[node] = int(st.get("term", 0) or 0)
        if claimants:
            time.sleep(0.3)  # confirmation re-poll for stability
            for node in RAFT_NODE_IDS:
                try:
                    st = client.status(node, timeout=0.6)
                except Exception:
                    st = None
                if st and st.get("role") == "leader":
                    term = int(st.get("term", 0) or 0)
                    if term > claimants.get(node, -1):
                        claimants[node] = term
            best = max(claimants, key=lambda n: claimants[n])
            return best, claimants[best]
        time.sleep(0.1)
    return None, 0


def _read_elections(nodes_dir: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for node in RAFT_NODE_IDS:
        path = nodes_dir / node / ("elections_%s.log" % node)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
    return history


def _read_stores(nodes_dir: Path) -> dict[str, dict[str, Any]]:
    stores: dict[str, dict[str, Any]] = {}
    for node in RAFT_NODE_IDS:
        try:
            state = json.loads((nodes_dir / node / "state.json").read_text(encoding="utf-8"))
            stores[node] = dict(state.get("store", {}))
        except (OSError, ValueError):
            stores[node] = {}
    return stores


def _wait_converged(nodes_dir: Path, timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    """Poll stores until all nodes agree (commit heartbeats need a beat)."""
    deadline = time.monotonic() + timeout
    last: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        last = _read_stores(nodes_dir)
        if len(last) == len(RAFT_NODE_IDS) and all(
            s == last[RAFT_NODE_IDS[0]] for s in last.values()
        ) and last[RAFT_NODE_IDS[0]]:
            return last
        time.sleep(0.2)
    return last


def _clear_runtime_state(*paths: Path) -> None:
    """Remove runtime artefacts left over from a previous (self-test) run.

    Called before each scenario so a model that exercised its own
    implementation inside the workspace is not graded on stale state
    (queued mailbox messages, node state/logs, service DBs, WALs,
    supervisor registries). Only generated runtime data is deleted —
    the model's source deliverable is never touched.
    """
    import shutil as _shutil

    for path in paths:
        try:
            if path.is_dir():
                _shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            pass


def run_raft_scenario(
    workspace_dir: str | Path,
    seed: int = 7,
    leader_timeout: float = 25.0,
) -> dict[str, Any]:
    """Execute the full Raft Jepsen scenario; returns milestone inputs.

    Never raises for model-behaviour reasons — every failure is captured
    as data (only harness-internal crashes propagate).
    """
    # Absolute: child node processes are spawned with cwd=node-dir, so any
    # relative script path would resolve against the WRONG directory
    # (doubled path, silent no-start, leader=None for every model).
    workspace_dir = Path(workspace_dir).resolve()
    broker_root = workspace_dir / "broker"
    nodes_dir = workspace_dir / "nodes"
    # Fresh run: wipe runtime state the model may have left behind while
    # self-testing (queued mailbox messages, node state/logs, supervisor
    # registry). Only the model's own source file is preserved, so a model
    # that legitimately exercised its implementation is not judged on
    # leftovers from its own rehearsal.
    _clear_runtime_state(broker_root, nodes_dir, workspace_dir / "sup")
    broker_root.mkdir(parents=True, exist_ok=True)
    nodes_dir.mkdir(parents=True, exist_ok=True)
    write_partitions(broker_root, [])

    raft_py = workspace_dir / "raft.py"
    data: dict[str, Any] = {
        "leader": None, "phase_a_acked": [], "phase_a_ops": [],
        "elections": [], "minority_write_acked": None,
        "partition_acked": [], "post_heal_stores": {}, "committed": [],
        "nemesis_ok": True, "nemesis_log": [], "kill_observed": False,
        "post_crash_acked": [], "final_stores": {}, "client_ops": [],
        "notes": [],
    }
    if not raft_py.exists():
        data["notes"].append("raft.py missing: no node implementation")
        return data

    broker = FileMessageBroker(str(broker_root), list(BUS_IDS), seed=seed)
    client = _RaftClient(broker)
    mgr = SupervisorManager(str(workspace_dir / "sup"))
    try:
        for node in RAFT_NODE_IDS:
            workdir = nodes_dir / node
            workdir.mkdir(parents=True, exist_ok=True)
            mgr.spawn_node(
                node,
                [sys.executable, str(raft_py), node, str(broker_root), str(workdir), str(seed)],
                workdir=str(workdir),
            )
        leader, _ = _wait_leader(client, leader_timeout)
        data["leader"] = leader
        if leader is None:
            data["notes"].append("no leader elected within %.0fs" % leader_timeout)
            return data

        # Phase A: steady-state replication (20 writes through the leader).
        for i in range(20):
            ok, req_id, start, end = client.write(leader, "k%02d" % i, i)
            data["phase_a_ops"].append(
                {"type": "write", "key": "k%02d" % i, "value": i,
                 "start": start, "end": end})
            if ok:
                data["client_ops"].append(
                    {"type": "write", "key": "k%02d" % i, "value": i,
                     "start": start, "end": end})
                data["phase_a_acked"].append({"k": "k%02d" % i, "v": i})

        # Phase B: split-brain — isolate the leader (minority of one).
        others = [n for n in RAFT_NODE_IDS if n != leader]

        def _do_partition(event) -> None:  # nemesis custom handler
            nodes_a, nodes_b = event.target, event.params["nodes_b"]
            broker.matrix.partition(nodes_a, nodes_b)
            cuts = [(a, b) for a in nodes_a for b in nodes_b]
            write_partitions(broker_root, cuts)

        def _do_heal(event) -> None:
            broker.matrix.heal_all()
            write_partitions(broker_root, [])

        nemesis = ChaosNemesis(
            ScenarioTimeline.from_tuples([(0.0, "partition", [leader], {"nodes_b": others})]),
            supervisor=mgr, broker=broker, seed=seed,
        )
        nemesis.register_handler("partition", _do_partition)
        nemesis.register_handler("heal", _do_heal)
        log = nemesis.run()
        data["nemesis_log"].extend(
            [{"action": r.action, "ok": r.ok, "error": r.error} for r in log])
        data["nemesis_ok"] = data["nemesis_ok"] and all(r.ok for r in log)
        time.sleep(1.5)  # let the majority notice + re-elect

        # Minority write must NOT commit (no ack within budget).
        ok_min, _, _, _ = client.write(leader, "minority_key", 1, timeout=1.5)
        data["minority_write_acked"] = ok_min

        # Majority side must elect a fresh leader and keep committing.
        new_leader, _ = _wait_leader(client, 8.0)
        data["partition_leader"] = new_leader
        majority = [n for n in RAFT_NODE_IDS if n != leader]
        if new_leader is not None and new_leader in majority:
            for i in range(5):
                ok, _, start, end = client.write(new_leader, "p%02d" % i, 100 + i)
                if ok:
                    data["client_ops"].append(
                        {"type": "write", "key": "p%02d" % i, "value": 100 + i,
                         "start": start, "end": end})
                    data["partition_acked"].append({"k": "p%02d" % i, "v": 100 + i})
        data["elections"] = _read_elections(nodes_dir)

        # Phase C: heal + catch-up.
        healer = ChaosNemesis(
            ScenarioTimeline.from_tuples([(0.0, "heal")]),
            supervisor=mgr, broker=broker, seed=seed,
        )
        healer.register_handler("heal", _do_heal)
        log = healer.run()
        data["nemesis_log"].extend(
            [{"action": r.action, "ok": r.ok, "error": r.error} for r in log])
        data["nemesis_ok"] = data["nemesis_ok"] and all(r.ok for r in log)
        time.sleep(1.0)  # allow log catch-up, then poll for agreement
        data["post_heal_stores"] = _wait_converged(nodes_dir)
        # Committed set = every acked write (phase A + partition window).
        data["committed"] = list(data["phase_a_acked"]) + list(data["partition_acked"])
        # Post-heal reads for the linearizability record.
        heal_leader, _ = _wait_leader(client, 8.0)
        data["heal_leader"] = heal_leader
        if heal_leader:
            for rec in data["committed"][:6]:
                value, ok = client.read(heal_leader, rec["k"])
                if ok:
                    data["client_ops"].append(
                        {"type": "read", "key": rec["k"], "value": value,
                         "start": time.monotonic() - 0.01, "end": time.monotonic()})

        # Phase D: marker-triggered external SIGKILL at the commit point.
        # The leader is re-resolved every iteration and every node log is
        # watched, so a stale heal_leader snapshot can neither hide the
        # commit nor shield the committer from the kill.
        if heal_leader:
            watchers = {
                node: mgr.watch_marker(node, CRITICAL_MARKER, action="kill",
                                       poll_interval=0.02)
                for node in RAFT_NODE_IDS
            }
            killed_node: str | None = None
            try:
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    current, _ = _wait_leader(client, 4.0)
                    target = current or heal_leader
                    ok, _, _, _ = client.write(target, "crash_probe", 7, timeout=3.0)
                    try:
                        states = mgr.statuses()
                    except Exception:
                        states = {}
                    for node, watcher in watchers.items():
                        st = states.get(node)
                        if watcher.fired_count > 0 or (
                                st is not None and st.state.value == "killed"):
                            killed_node = node
                    if killed_node is not None:
                        data["kill_observed"] = True
                        break
                    if ok:
                        data["post_crash_acked"].append({"k": "crash_probe", "v": 7})
                        # The marker kill may land just after the ack round
                        # trip; give the watchers one last beat to report it.
                        time.sleep(0.6)
                        try:
                            states = mgr.statuses()
                        except Exception:
                            states = {}
                        for node, watcher in watchers.items():
                            st = states.get(node)
                            if watcher.fired_count > 0 or (
                                    st is not None and st.state.value == "killed"):
                                killed_node = node
                        if killed_node is not None:
                            data["kill_observed"] = True
                        break
                    time.sleep(0.2)
                time.sleep(0.5)
            finally:
                for watcher in watchers.values():
                    try:
                        watcher.stop(timeout=5.0)
                    except Exception:
                        pass
                # 确保被击杀的节点完全停止，消除异步 kill 与 restart 之间的竞态
                time.sleep(0.5)
            # Restart the killed node (state dir preserved -> recovery).
            data["killed_node"] = killed_node or heal_leader
            try:
                mgr.restart_node(data["killed_node"], graceful=False, stop_timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                data["notes"].append("restart failed: %r" % (exc,))
            time.sleep(2.0)
            # 15s: Windows cold-start / disk-handle jitter after the external
            # SIGKILL restart can exceed the previous 10s budget.
            final_leader, _ = _wait_leader(client, 15.0)
            data["final_leader"] = final_leader
            if final_leader:
                for i in range(3):
                    ok, _, start, end = client.write(final_leader, "f%02d" % i, 200 + i)
                    if ok:
                        data["client_ops"].append(
                            {"type": "write", "key": "f%02d" % i, "value": 200 + i,
                             "start": start, "end": end})
                        data["committed"].append({"k": "f%02d" % i, "v": 200 + i})
                        data["post_crash_acked"].append({"k": "f%02d" % i, "v": 200 + i})
        data["elections"] = _read_elections(nodes_dir)
        data["final_stores"] = _wait_converged(nodes_dir)
        return data
    finally:
        try:
            mgr.shutdown(timeout=10.0)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Saga service simulator (parent-side threads; survive coordinator SIGKILL)
# ---------------------------------------------------------------------------

SAGA_BUS = (COORD_ID, "client") + SAGA_SERVICES
SAGA_ITEMS = {"alpha": 100, "beta": 100}


class _StoreUnavailable(Exception):
    pass


class ServiceSimulator(threading.Thread):
    """SQLite-backed saga participant speaking the coordinator protocol.

    All sends flow through one shared :class:`FileMessageBroker` (passed
    in) so envelope filenames stay unique; every send/recv honours
    ``partitions.json``. Failures of the SQLite store are caught and
    reported as ``ok: False`` (fail-closed) — the thread never dies.
    """

    def __init__(
        self,
        svc_id: str,
        broker_root: str | Path,
        db_path: str | Path,
        sender: FileMessageBroker,
        seed: int = 0,
    ) -> None:
        super().__init__(daemon=True, name="svc-%s" % svc_id)
        self.svc_id = svc_id
        self.broker_root = str(broker_root)
        self.sender = sender
        self.reader = FileMessageBroker(str(broker_root), list(SAGA_BUS), seed=seed)
        self.db_path = str(db_path)
        self._db_lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._guard = threading.Lock()
        self._aborted: set[str] = set()
        self._stop = threading.Event()
        self._cuts: set[tuple[str, str]] = set()
        self._cuts_ts = 0.0
        self._init_db()

    # -- store -----------------------------------------------------------
    def _init_db(self) -> None:
        con = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS stock (item TEXT PRIMARY KEY, qty INTEGER)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS holds (txid TEXT, item TEXT, qty INTEGER, state TEXT)"
            )
            con.execute("CREATE TABLE IF NOT EXISTS done (txid TEXT PRIMARY KEY, outcome TEXT)")
            for item, qty in SAGA_ITEMS.items():
                con.execute("INSERT OR IGNORE INTO stock (item, qty) VALUES (?, ?)", (item, qty))
            con.commit()
        finally:
            con.close()

    def _db(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self.db_path, timeout=5.0)
        except sqlite3.Error as exc:
            raise _StoreUnavailable(str(exc)) from exc

    # -- item locks --------------------------------------------------------
    def _entry(self, item: str) -> dict[str, Any]:
        with self._guard:
            entry = self._entries.get(item)
            if entry is None:
                entry = {"holder": None, "waiters": {}, "cond": threading.Condition(self._guard)}
                self._entries[item] = entry
            return entry

    # -- messaging ----------------------------------------------------------
    def _blocked(self, other: str) -> bool:
        now = time.monotonic()
        if now - self._cuts_ts > 0.05:
            self._cuts = read_partitions(self.broker_root)
            self._cuts_ts = now
        return _cut_blocks(self._cuts, self.svc_id, other)

    def _reply(self, dst: str, msg: dict[str, Any]) -> None:
        if self._blocked(dst):
            return
        try:
            self.sender.send(self.svc_id, dst, msg)
        except (OSError, TypeError, KeyError):
            pass

    # -- main loop ------------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                incoming = self.reader.recv(self.svc_id)
            except Exception:
                incoming = []
            for src, msg in incoming:
                if not isinstance(msg, dict) or _cut_blocks(read_partitions(self.broker_root), src, self.svc_id):
                    continue
                worker = threading.Thread(target=self._handle, args=(src, msg), daemon=True)
                worker.start()
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop.set()

    # -- request handling -------------------------------------------------------
    def _handle(self, src: str, msg: dict[str, Any]) -> None:
        req_id = msg.get("req_id")
        kind = msg.get("type")
        try:
            if kind == "prepare":
                ok, reason = self._do_prepare(msg.get("txid", ""), msg.get("item", ""), int(msg.get("qty", 0)))
                self._reply(src, {"type": "prepare_ack", "req_id": req_id, "ok": ok, "reason": reason})
            elif kind == "commit":
                ok, reason = self._do_commit(msg.get("txid", ""))
                self._reply(src, {"type": "commit_ack", "req_id": req_id, "ok": ok, "reason": reason})
            elif kind == "rollback":
                self._do_rollback(msg.get("txid", ""))
                self._reply(src, {"type": "rollback_ack", "req_id": req_id, "ok": True})
            elif kind == "status":
                self._reply(src, {"type": "status_ack", "req_id": req_id,
                                  "ok": True,
                                  "state": self._do_status(msg.get("txid", ""))})
            elif kind == "waits":
                holders, waiters = self._do_waits()
                self._reply(src, {"type": "waits_ack", "req_id": req_id,
                                  "ok": True,
                                  "holders": holders, "waiters": waiters})
        except _StoreUnavailable as exc:
            try:
                self._reply(src, {"type": kind + "_ack", "req_id": req_id,
                                  "ok": False, "reason": "store-unavailable: %s" % exc})
            except Exception:
                pass
        except Exception as exc:  # never kill the service thread
            try:
                self._reply(src, {"type": (kind or "unknown") + "_ack", "req_id": req_id,
                                  "ok": False, "reason": "%r" % (exc,)})
            except Exception:
                pass

    def _do_prepare(self, txid: str, item: str, qty: int) -> tuple[bool, str]:
        entry = self._entry(item)
        cond = entry["cond"]
        deadline = time.monotonic() + 2.0
        with self._guard:
            entry["waiters"][txid] = item
            while entry["holder"] not in (None, txid):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                cond.wait(timeout=min(remaining, 0.05))
            entry["waiters"].pop(txid, None)
            if entry["holder"] not in (None, txid):
                return False, "lock-timeout"
            entry["holder"] = txid
        if txid in self._aborted:
            self._release(item, txid)
            return False, "aborted"
        with self._db_lock:
            try:
                con = self._db()
            except _StoreUnavailable as exc:
                self._release(item, txid)
                raise
            try:
                stock = con.execute("SELECT qty FROM stock WHERE item=?", (item,)).fetchone()
                held = con.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM holds WHERE item=? AND txid!=? AND state='prepared'",
                    (item, txid)).fetchone()[0]
                if stock is None or stock[0] - held < qty:
                    con.close()
                    self._release(item, txid)
                    return False, "insufficient"
                con.execute("DELETE FROM holds WHERE txid=? AND item=?", (txid, item))
                con.execute("INSERT INTO holds VALUES (?,?,?,'prepared')", (txid, item, qty))
                con.commit()
                con.close()
            except sqlite3.DatabaseError as exc:
                try:
                    con.close()
                except Exception:
                    pass
                self._release(item, txid)
                raise _StoreUnavailable(str(exc)) from exc
        return True, "prepared"

    def _release(self, item: str, txid: str) -> None:
        entry = self._entry(item)
        with self._guard:
            if entry["holder"] == txid:
                entry["holder"] = None
            entry["waiters"].pop(txid, None)
            entry["cond"].notify_all()

    def _do_commit(self, txid: str) -> tuple[bool, str]:
        with self._db_lock:
            try:
                con = self._db()
            except _StoreUnavailable as exc:
                raise
            try:
                row = con.execute("SELECT outcome FROM done WHERE txid=?", (txid,)).fetchone()
                if row is not None:
                    con.close()
                    return (row[0] == "committed", "duplicate")
                holds = con.execute("SELECT item, qty FROM holds WHERE txid=? AND state='prepared'",
                                    (txid,)).fetchall()
                if not holds:
                    con.close()
                    return False, "unknown-hold"
                for item, qty in holds:
                    con.execute("UPDATE stock SET qty = qty - ? WHERE item=?", (qty, item))
                con.execute("DELETE FROM holds WHERE txid=?", (txid,))
                con.execute("INSERT INTO done VALUES (?,'committed')", (txid,))
                con.commit()
                con.close()
            except sqlite3.DatabaseError as exc:
                try:
                    con.close()
                except Exception:
                    pass
                raise _StoreUnavailable(str(exc)) from exc
        for item, _ in holds:
            self._release(item, txid)
        return True, "committed"

    def _do_rollback(self, txid: str) -> None:
        with self._db_lock:
            try:
                con = self._db()
            except _StoreUnavailable:
                self._aborted.add(txid)
                return
            try:
                items = [r[0] for r in con.execute("SELECT DISTINCT item FROM holds WHERE txid=?", (txid,))]
                con.execute("DELETE FROM holds WHERE txid=?", (txid,))
                row = con.execute("SELECT outcome FROM done WHERE txid=?", (txid,)).fetchone()
                if row is None:
                    con.execute("INSERT INTO done VALUES (?,'aborted')", (txid,))
                con.commit()
                con.close()
            except sqlite3.DatabaseError:
                try:
                    con.close()
                except Exception:
                    pass
                self._aborted.add(txid)
                return
        self._aborted.add(txid)
        for item in items:
            self._release(item, txid)

    def _do_status(self, txid: str) -> str:
        with self._db_lock:
            try:
                con = self._db()
            except _StoreUnavailable:
                return "store-unavailable"
            try:
                row = con.execute("SELECT outcome FROM done WHERE txid=?", (txid,)).fetchone()
                if row is not None:
                    con.close()
                    return row[0]
                hold = con.execute("SELECT state FROM holds WHERE txid=? LIMIT 1", (txid,)).fetchone()
                con.close()
                return hold[0] if hold else "unknown"
            except sqlite3.DatabaseError:
                try:
                    con.close()
                except Exception:
                    pass
                return "store-unavailable"

    def _do_waits(self) -> tuple[dict[str, str], dict[str, str]]:
        with self._guard:
            holders = {item: e["holder"] for item, e in self._entries.items() if e["holder"]}
            waiters: dict[str, str] = {}
            for item, e in self._entries.items():
                for txid in e["waiters"]:
                    waiters[txid] = item
        return holders, waiters


# ---------------------------------------------------------------------------
# Reference saga.py (coordinator honouring the harness contract)
# ---------------------------------------------------------------------------

REFERENCE_SAGA = '''"""Saga distributed-transaction coordinator (reference, stdlib only).

Harness contract:
  class Coordinator(broker_root, services, wal_path, node_id="coord",
                    timeout=2.5, seed=0)
  op: {"service": <id>, "item": <str>, "qty": <int>}
  begin(ops) -> txid | prepare(txid) -> {svc: bool}
  prepare_more(txid, extra_ops) -> {svc: bool} | commit(txid, hold_open=0.0)
  rollback(txid, reason="") | execute(ops) -> (ok, txid)
  recover() -> [resolved txids] (idempotent)
  detect_deadlock(graph) -> cycle|None (static) | resolve_deadlocks()
  commit prints CRITICAL_WRITE_POINT before the durable commit sends.
Participants honour <broker_root>/partitions.json bidirectional cuts.
"""

import json
import os
import random
import sys
import time
import uuid


def _read_cuts(root):
    try:
        with open(os.path.join(root, "partitions.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return {(str(a), str(b)) for a, b in data.get("cuts", [])}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


class Coordinator:
    def __init__(self, broker_root, services, wal_path, node_id="coord",
                 timeout=2.5, seed=0):
        self.root = str(broker_root)
        self.services = list(services)
        self.wal_path = str(wal_path)
        self.node_id = node_id
        self.timeout = float(timeout)
        self.rng = random.Random(seed)
        self._seq = 0
        self._buffer = []
        self._txns = {}
        self._order = []
        try:
            from benchmark_v3.bench_harness.jepsen.broker import FileMessageBroker
        except Exception as exc:
            raise RuntimeError("saga coordinator needs benchmark_v3 on sys.path: %r" % (exc,))
        bus = [node_id] + [s for s in ("coord", "client") if s != node_id] + list(services)
        seen, dedup = [], set()
        for b in bus:
            if b not in dedup:
                dedup.add(b)
                seen.append(b)
        self.bus = FileMessageBroker(str(broker_root), seen, seed=seed)

    # -- wire -----------------------------------------------------------
    def _blocked(self, other):
        cuts = _read_cuts(self.root)
        return (self.node_id, other) in cuts or (other, self.node_id) in cuts

    def _send(self, dst, msg):
        if self._blocked(dst) or self.bus is None:
            return False
        try:
            return self.bus.send(self.node_id, dst, msg) is not None
        except (OSError, TypeError, KeyError, ValueError):
            return False

    def _collect(self, req_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.bus is not None:
                try:
                    for src, msg in self.bus.recv(self.node_id):
                        if isinstance(msg, dict) and msg.get("req_id") == req_id:
                            return msg
                        self._buffer.append((src, msg))
                except (OSError, ValueError):
                    pass
            for i, (src, msg) in enumerate(self._buffer):
                if isinstance(msg, dict) and msg.get("req_id") == req_id:
                    return self._buffer.pop(i)[1]
            time.sleep(0.02)
        return None

    def _request(self, svc, msg, timeout=None):
        msg = dict(msg)
        self._seq += 1
        msg["req_id"] = "%s-%d-%s" % (self.node_id, self._seq, uuid.uuid4().hex[:6])
        self._send(svc, msg)
        return self._collect(msg["req_id"], self.timeout if timeout is None else timeout)

    # -- write-ahead log --------------------------------------------------
    def _wal(self, event):
        try:
            with open(self.wal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        except OSError:
            pass

    def _read_wal(self):
        events = []
        try:
            with open(self.wal_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return events

    # -- transaction API ----------------------------------------------------
    def begin(self, ops):
        txid = "tx-%s" % uuid.uuid4().hex[:12]
        self._txns[txid] = list(ops)
        self._order.append(txid)
        self._wal({"event": "begin", "txid": txid, "ops": list(ops)})
        return txid

    def _prepare_ops(self, txid, ops):
        results = {}
        for op in ops:
            svc = op["service"]
            reply = self._request(svc, {"type": "prepare", "txid": txid,
                                        "item": op["item"], "qty": int(op.get("qty", 1))})
            results[svc] = bool(reply and reply.get("ok"))
            if not results[svc]:
                break
        return results

    def prepare(self, txid):
        results = self._prepare_ops(txid, self._txns.get(txid, []))
        self._wal({"event": "prepared", "txid": txid, "results": results})
        return results

    def prepare_more(self, txid, extra_ops):
        self._txns.setdefault(txid, []).extend(extra_ops)
        self._wal({"event": "begin", "txid": txid, "ops": list(extra_ops)})
        return self._prepare_ops(txid, extra_ops)

    def commit(self, txid, hold_open=0.0):
        self._wal({"event": "commit-start", "txid": txid})
        print("CRITICAL_WRITE_POINT", flush=True)
        if hold_open > 0:
            time.sleep(hold_open)
        acks = {}
        for op in self._txns.get(txid, []):
            svc = op["service"]
            if svc in acks:
                continue
            ok = False
            for _ in range(3):
                reply = self._request(svc, {"type": "commit", "txid": txid})
                if reply and reply.get("ok"):
                    ok = True
                    break
            acks[svc] = ok
        self._wal({"event": "committed", "txid": txid, "acks": acks})
        return acks

    def rollback(self, txid, reason=""):
        for op in self._txns.get(txid, []):
            self._request(op["service"], {"type": "rollback", "txid": txid},
                           timeout=min(self.timeout, 1.5))
        self._wal({"event": "aborted", "txid": txid, "reason": reason})
        return True

    def execute(self, ops):
        txid = self.begin(ops)
        results = self.prepare(txid)
        if all(results.values()) and results:
            self.commit(txid)
            return True, txid
        self.rollback(txid, "prepare-failed: %r" % (results,))
        return False, txid

    def recover(self):
        begun, prepared, finished = {}, set(), set()
        for ev in self._read_wal():
            kind, txid = ev.get("event"), ev.get("txid")
            if kind == "begin" and txid:
                begun.setdefault(txid, []).extend(ev.get("ops", []))
                if txid not in self._txns:
                    self._txns[txid] = list(begun[txid])
                    self._order.append(txid)
            elif kind in ("prepared", "commit-start") and txid:
                prepared.add(txid)
            elif kind in ("committed", "aborted") and txid:
                finished.add(txid)
        resolved = []
        for txid in prepared - finished:
            states = set()
            for op in begun.get(txid, []):
                reply = self._request(op["service"], {"type": "status", "txid": txid},
                                      timeout=min(self.timeout, 1.5))
                states.add(reply.get("state") if reply else "unknown")
            if states and states <= {"prepared", "committed"}:
                self.commit(txid)
            else:
                self.rollback(txid, "recovery: incomplete prepares")
            resolved.append(txid)
        return resolved

    # -- deadlock handling ----------------------------------------------------
    @staticmethod
    def detect_deadlock(graph):
        visited, stack, order = set(), [], []

        def visit(node, path):
            visited.add(node)
            stack.append(node)
            for nxt in graph.get(node, []):
                if nxt in stack:
                    return stack[stack.index(nxt):] + [nxt]
                if nxt not in visited:
                    hit = visit(nxt, path + [nxt])
                    if hit:
                        return hit
            stack.pop()
            return None

        for node in graph:
            if node not in visited:
                hit = visit(node, [node])
                if hit:
                    return hit
        return None

    def resolve_deadlocks(self):
        holders, waiters = {}, {}
        for svc in self.services:
            reply = self._request(svc, {"type": "waits"}, timeout=min(self.timeout, 1.5))
            if not reply:
                continue
            for item, holder in (reply.get("holders") or {}).items():
                holders["%s:%s" % (svc, item)] = holder
            for txid, item in (reply.get("waiters") or {}).items():
                holder = holders.get("%s:%s" % (svc, item))
                if holder and holder != txid:
                    waiters.setdefault(txid, []).append(holder)
        cycle = self.detect_deadlock(waiters)
        if not cycle:
            return None
        members = [c for c in dict.fromkeys(cycle)]
        victim = max(members, key=lambda t: self._order.index(t) if t in self._order else -1)
        self.rollback(victim, "deadlock-victim")
        return cycle
'''

#: Harness-side saga phase runner (imports the model's saga.py).
SAGA_RUNNER = '''"""Saga scenario phase runner (harness code; imports model saga.py)."""
import json
import os
import sys
import time

workspace, phase = sys.argv[1], sys.argv[2]
site_dir = sys.argv[3] if len(sys.argv) > 3 else None
if site_dir:
    sys.path.insert(0, site_dir)
sys.path.insert(0, workspace)
from saga import Coordinator

SERVICES = ["inventory", "payment", "shipping"]
broker_root = os.path.join(workspace, "saga_broker")
wal_path = os.path.join(workspace, "saga_wal.jsonl")

BATCH1 = [
    [{"service": "inventory", "item": "alpha", "qty": 2},
     {"service": "payment", "item": "alpha", "qty": 2},
     {"service": "shipping", "item": "alpha", "qty": 1}],
    [{"service": "inventory", "item": "beta", "qty": 3},
     {"service": "payment", "item": "beta", "qty": 1}],
    [{"service": "shipping", "item": "beta", "qty": 4}],
]
BIG = [{"service": "inventory", "item": "alpha", "qty": 5},
       {"service": "payment", "item": "beta", "qty": 5},
       {"service": "shipping", "item": "alpha", "qty": 5}]
BATCH2 = [
    [{"service": "inventory", "item": "alpha", "qty": 1},
     {"service": "shipping", "item": "beta", "qty": 1}],
    [{"service": "payment", "item": "alpha", "qty": 1}],
]


def _write(name, obj):
    path = os.path.join(workspace, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.write("\\n")
    os.replace(tmp, path)


if phase == "phase1":
    coord = Coordinator(broker_root, SERVICES, wal_path, node_id="coord", timeout=2.5)
    out = {"txns": []}
    for ops in BATCH1:
        ok, txid = coord.execute(ops)
        out["txns"].append({"ok": ok, "txid": txid, "ops": ops})
    _write("results_phase1a.json", out)
    big = coord.begin(BIG)
    prep = coord.prepare(big)
    out["big"] = {"txid": big, "prepared": prep, "ops": BIG}
    _write("results_phase1a.json", out)
    time.sleep(1.0)  # settle: let the harness arm its marker watcher
    coord.commit(big, hold_open=1.5)
    out["big_committed"] = True
    _write("results_phase1.json", out)
    print(json.dumps({"phase": 1, "ok": True}))
elif phase == "phase2":
    coord = Coordinator(broker_root, SERVICES, wal_path, node_id="coord", timeout=2.5)
    first = coord.recover()
    second = coord.recover()
    txns = []
    for ops in BATCH2:
        ok, txid = coord.execute(ops)
        txns.append({"ok": ok, "txid": txid, "ops": ops})
    _write("results_phase2.json", {"recovered": first, "recovered_again": second, "txns": txns})
    print(json.dumps({"phase": 2, "recovered": first, "again": second}))
'''


def _ms2(task_id: str, idx: int, name: str, checks: list[tuple[bool, str]]) -> MilestoneResult:
    """One milestone from exactly 2 sub-assertions (score = fraction passed)."""
    assert len(checks) == 2, "each long-task milestone carries 2 assertions"
    passed = all(c[0] for c in checks)
    detail = "; ".join("(%s) %s" % ("PASS" if c[0] else "FAIL", c[1]) for c in checks)
    return mk_milestone(
        "%s_m%02d" % (task_id, idx), name, passed,
        score=sum(1.0 for c in checks if c[0]) / 2.0,
        failure_reason=None if passed else detail, diagnostics=detail,
    )


def _svc_statuses(broker_root: Path, services: tuple[str, ...], txids: list[str],
                  timeout: float = 2.0) -> dict[str, dict[str, str]]:
    """Query every service for every txid via a throwaway client bus user."""
    bus = FileMessageBroker(str(broker_root), list(SAGA_BUS), seed=999)
    states: dict[str, dict[str, str]] = {svc: {} for svc in services}
    seq = 0
    for svc in services:
        for txid in txids:
            seq += 1
            req_id = "probe-%d" % seq
            try:
                bus.send("client", svc, {"type": "status", "txid": txid, "req_id": req_id})
            except (OSError, TypeError, KeyError):
                states[svc][txid] = "send-failed"
                continue
            deadline = time.monotonic() + timeout
            found = "unknown"
            while time.monotonic() < deadline:
                for _, msg in bus.recv("client"):
                    if isinstance(msg, dict) and msg.get("req_id") == req_id:
                        found = str(msg.get("state", "unknown"))
                        break
                if found != "unknown" or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            states[svc][txid] = found
    # drain our mailbox so later phases start clean
    try:
        bus.recv("client")
    except Exception:
        pass
    return states


# ---------------------------------------------------------------------------
# Out-of-process coordinator probes (child interpreter + JSON back-pattern)
# ---------------------------------------------------------------------------
#
# ``saga.py`` is the model's deliverable: importing it in the parent and
# calling ``Coordinator(...)`` lets a poisoned module kill (``sys.exit`` /
# ``os._exit``), corrupt or hang the harness. These probes therefore run the
# coordinator inside a child Python interpreter via :class:`ProcessRunner`.
# The parent writes the probe parameters as JSON argv, the child imports
# saga.py, drives the scenario against the SAME file-broker mailboxes (the
# parent-side service threads keep running and see the child like any other
# participant), and prints exactly one JSON verdict line on stdout. The
# parent takes the last ``{...}`` line; timeout / crash / non-JSON all
# degrade to ``None`` (probe isolated, milestone fails closed).

_SAGA_PROBE_EXECUTE = '''"""Saga child probe: single execute() through the model coordinator."""
import json, sys, time

workspace = sys.argv[1]
site_dir = sys.argv[2] if len(sys.argv) > 2 else None
payload = json.loads(sys.argv[3])
if site_dir:
    sys.path.insert(0, site_dir)
sys.path.insert(0, workspace)
from saga import Coordinator

out = {"ok": False}

def _finish():
    print(json.dumps(out), flush=True)

try:
    coord = Coordinator(payload["broker_root"], payload["services"],
                        payload["wal_path"], node_id=payload["node_id"],
                        timeout=payload["timeout"], seed=payload["seed"])
    ok, txid = coord.execute(payload["ops"])
    out["ok"] = bool(ok)
    out["txid"] = txid
except BaseException as exc:
    out["ok"] = False
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
_finish()
'''

_SAGA_PROBE_DEADLOCK = '''"""Saga child probe: scripted TA/TB deadlock + resolution verdict."""
import json, sys, threading, time

workspace = sys.argv[1]
site_dir = sys.argv[2] if len(sys.argv) > 2 else None
payload = json.loads(sys.argv[3])
if site_dir:
    sys.path.insert(0, site_dir)
sys.path.insert(0, workspace)
from saga import Coordinator

out = {}

def _finish():
    print(json.dumps(out), flush=True)

def _status(coord, txid):
    reply = coord._request("inventory", {"type": "status", "txid": txid},
                           timeout=min(payload["timeout"], 1.5))
    return str(reply.get("state")) if reply else "unknown"

try:
    dl = Coordinator(payload["broker_root"], payload["services"],
                     payload["wal_path"], node_id=payload["node_id"],
                     timeout=payload["timeout"], seed=payload["seed"])
    tx_a = dl.begin([{"service": "inventory", "item": "alpha", "qty": 1}])
    tx_b = dl.begin([{"service": "inventory", "item": "beta", "qty": 1}])
    prep_a = dl.prepare(tx_a)
    prep_b = dl.prepare(tx_b)
    out["deadlock_base"] = {"a": prep_a, "b": prep_b}
    results = {}

    def _more_a():
        results["a"] = dl.prepare_more(
            tx_a, [{"service": "inventory", "item": "beta", "qty": 1}])

    def _more_b():
        results["b"] = dl.prepare_more(
            tx_b, [{"service": "inventory", "item": "alpha", "qty": 1}])

    ta = threading.Thread(target=_more_a, daemon=True)
    tb = threading.Thread(target=_more_b, daemon=True)
    ta.start()
    tb.start()
    # Poll the wire-level waits view until both transaction threads are
    # blocked in a cycle (bounded wait budget; no hardcoded sleep).
    t_wait = time.monotonic() + float(payload["wait_budget"])
    while time.monotonic() < t_wait:
        if ta.is_alive() and tb.is_alive():
            reply = dl._request("inventory", {"type": "waits"},
                                timeout=min(payload["timeout"], 1.5))
            waiters = (reply or {}).get("waiters") or {}
            if len(waiters) >= 2:
                break
        time.sleep(0.05)
    cycle = dl.resolve_deadlocks()
    out["deadlock_cycle"] = cycle
    ta.join(timeout=float(payload["join_budget"]))
    tb.join(timeout=float(payload["join_budget"]))
    out["threads_settled"] = (not ta.is_alive()) and (not tb.is_alive())
    # Commit whichever side survived resolution (still 'prepared').
    for tx in (tx_a, tx_b):
        if _status(dl, tx) == "prepared":
            dl.commit(tx)
    time.sleep(0.3)
    inv_a = _status(dl, tx_a)
    inv_b = _status(dl, tx_b)
    out["survivor_committed"] = (inv_a == "committed") != (inv_b == "committed")
    out["victim_rolled_back"] = "aborted" in (inv_a, inv_b)
    out["deadlock_pair"] = [tx_a, tx_b]
except BaseException as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
_finish()
'''

_SAGA_PROBE_SCRIPTS = {
    "execute": _SAGA_PROBE_EXECUTE,
    "deadlock": _SAGA_PROBE_DEADLOCK,
}

#: Wall-clock budget for the whole deadlock child scenario (the in-child
#: budgets sum well below this; anything beyond means the child is hung).
SAGA_DEADLOCK_TIMEOUT = 60.0

#: Wall-clock budget for a single execute() probe child.
SAGA_EXECUTE_TIMEOUT = 30.0


def _run_saga_probe_in_child(
    workspace_dir: Path,
    op: str,
    payload: dict[str, Any],
    site_dir: str | None = None,
    timeout: float = SAGA_EXECUTE_TIMEOUT,
) -> dict[str, Any] | None:
    """Run one coordinator probe in a child interpreter; never raises.

    The child imports the model's ``saga.py``, executes *op* and prints one
    JSON object. Returns the parsed dict, or ``None`` on timeout / crash /
    non-JSON output — the parent harness always survives model bugs.
    """
    script = _SAGA_PROBE_SCRIPTS.get(op)
    if script is None:
        return None
    runner = ProcessRunner(default_timeout=timeout)
    # 同 reviewer 探针：子进程以 cwd=workspace_dir 启动，必须先解析为绝对
    # 路径，否则相对路径会被相对 cwd 再拼一次（路径重复 -> 探针全部失败）。
    result = runner.run(
        [sys.executable, "-c", script, str(Path(workspace_dir).resolve()),
         str(site_dir or ""), json.dumps(payload)],
        cwd=workspace_dir,
        timeout=timeout,
    )
    if result.timed_out:
        return None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def run_saga_scenario(
    workspace_dir: str | Path,
    seed: int = 11,
    coord_src: str | None = None,
) -> dict[str, Any]:
    """Execute the full Saga Jepsen scenario; returns milestone inputs."""
    # Absolute (see run_raft_scenario): coord children spawn with their own
    # cwd, so relative runner/workspace paths would silently no-start.
    workspace_dir = Path(workspace_dir).resolve()
    # Fresh run: wipe runtime state from any self-test the model performed
    # (broker queues, coord workdirs, service DBs, WALs, supervisor registry).
    # `saga.py` (the deliverable) and `run_saga.py` (rewritten below) survive.
    _clear_runtime_state(
        workspace_dir / "saga_broker",
        workspace_dir / "sup",
        workspace_dir / "coord1",
        workspace_dir / "coord2",
        workspace_dir / "payment.db",
        workspace_dir / "payment.db.bak",
        workspace_dir / "inventory.db",
        workspace_dir / "shipping.db",
        workspace_dir / "saga_wal.jsonl",
        workspace_dir / "probe_wal.jsonl",
        workspace_dir / "deadlock_wal.jsonl",
        workspace_dir / "results_phase1.json",
        workspace_dir / "results_phase1a.json",
        workspace_dir / "results_phase2.json",
    )
    broker_root = workspace_dir / "saga_broker"
    broker_root.mkdir(parents=True, exist_ok=True)
    write_partitions(broker_root, [])
    site_dir = str(Path(__file__).resolve().parents[3])

    if coord_src is not None:
        (workspace_dir / "saga.py").write_text(coord_src, encoding="utf-8")
    (workspace_dir / "run_saga.py").write_text(SAGA_RUNNER, encoding="utf-8")

    data: dict[str, Any] = {
        "phase1a": None, "big_txid": None, "kill_observed": False,
        "phase2": None, "nemesis_log": [], "nemesis_ok": True,
        "torn_probe_aborted": False, "svc_alive_after_corrupt": False,
        "partition_probe_aborted": False, "healed_probe_ok": False,
        "deadlock_cycle": None, "survivor_committed": False,
        "victim_rolled_back": False, "dangling_holds": None,
        "service_states": {}, "committed_txids": [], "notes": [],
    }
    if not (workspace_dir / "saga.py").exists():
        data["notes"].append("saga.py missing: no coordinator implementation")
        return data

    sender = FileMessageBroker(str(broker_root), list(SAGA_BUS), seed=seed)
    services = [
        ServiceSimulator(svc, broker_root, workspace_dir / ("%s.db" % svc), sender, seed + i)
        for i, svc in enumerate(SAGA_SERVICES)
    ]
    for svc in services:
        svc.start()
    time.sleep(0.3)
    mgr = SupervisorManager(str(workspace_dir / "sup"))
    try:
        runner = str(workspace_dir / "run_saga.py")

        # Phase 1: steady txns, then big commit killed at the marker.
        # The watcher arms only after phase1a reports the big txn prepared,
        # and starts counting from the CURRENT end of the log: BATCH1's three
        # commits printed markers long before this point, so `from_start=True`
        # would fire on a stale marker and kill the coordinator before the
        # `big` transaction was even prepared.
        coord1 = mgr.spawn_node(
            "coord1", [sys.executable, runner, str(workspace_dir), "phase1", site_dir],
            workdir=str(workspace_dir / "coord1"),
        )
        phase1a = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            candidate = workspace_dir / "results_phase1a.json"
            if candidate.exists():
                try:
                    loaded = json.loads(candidate.read_text(encoding="utf-8"))
                    if loaded.get("txns"):
                        phase1a = loaded
                        if isinstance(loaded.get("big"), dict):
                            break
                except ValueError:
                    pass
            time.sleep(0.1)
        # from_start=False: ignore every marker already written by the BATCH1
        # commits; only the upcoming `big` commit may trigger the kill.
        watcher = mgr.watch_marker("coord1", CRITICAL_MARKER, action="kill", poll_interval=0.02, from_start=False)
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            candidate = workspace_dir / "results_phase1a.json"
            if candidate.exists():
                try:
                    phase1a = json.loads(candidate.read_text(encoding="utf-8"))
                except ValueError:
                    phase1a = None
            try:
                st = mgr.statuses()["coord1"]
            except KeyError:
                st = None
            if (st is not None and st.state.value == "killed") or watcher.fired_count > 0:
                data["kill_observed"] = True
                break
            if (workspace_dir / "results_phase1.json").exists():
                break  # commit slipped through before the kill landed
            time.sleep(0.1)
        watcher.stop()
        data["phase1a"] = phase1a
        if phase1a and isinstance(phase1a.get("big"), dict):
            data["big_txid"] = phase1a["big"].get("txid")
        try:
            mgr.stop_all(timeout=5.0)
        except Exception:
            pass

        # Phase 2: fresh coordinator recovers the WAL, then keeps serving.
        try:
            mgr.spawn_node("coord2", [sys.executable, runner, str(workspace_dir), "phase2", site_dir],
                           workdir=str(workspace_dir / "coord2"))
        except RuntimeError:
            pass
        deadline = time.monotonic() + 40.0
        phase2 = None
        while time.monotonic() < deadline:
            candidate = workspace_dir / "results_phase2.json"
            if candidate.exists():
                try:
                    phase2 = json.loads(candidate.read_text(encoding="utf-8"))
                    break
                except ValueError:
                    pass
            time.sleep(0.1)
        data["phase2"] = phase2
        try:
            mgr.stop_all(timeout=5.0)
        except Exception:
            pass

        # Fault plane via ChaosNemesis: torn write, then partition/heal.
        pay_db = workspace_dir / "payment.db"
        backup = workspace_dir / "payment.db.bak"
        try:
            import shutil as _shutil

            _shutil.copyfile(pay_db, backup)
        except OSError as exc:
            data["notes"].append("backup failed: %r" % (exc,))

        def _do_partition(event) -> None:
            nodes_a, nodes_b = event.target, event.params["nodes_b"]
            sender.matrix.partition(nodes_a, nodes_b)
            write_partitions(broker_root, [(a, b) for a in nodes_a for b in nodes_b])

        def _do_heal(event) -> None:
            sender.matrix.heal_all()
            write_partitions(broker_root, [])

        faults = ChaosNemesis(
            ScenarioTimeline.from_tuples([
                (0.0, "corrupt_file", str(pay_db), {"mode": "truncate", "size": 0}),
                (0.2, "partition", [COORD_ID, "client"], {"nodes_b": ["payment"]}),
                (1.0, "heal"),
            ]),
            supervisor=mgr, broker=sender, seed=seed,
        )
        faults.register_handler("partition", _do_partition)
        faults.register_handler("heal", _do_heal)
        for record in faults.run():
            data["nemesis_log"].append({"action": record.action, "ok": record.ok, "error": record.error})
        data["nemesis_ok"] = all(r["ok"] for r in data["nemesis_log"])

        # Torn-write probe (partition still open -> payment isolated too;
        # heal first so the abort is attributable to the corrupt store).
        write_partitions(broker_root, [])
        sender.matrix.heal_all()
        time.sleep(0.2)
        data["svc_alive_after_corrupt"] = any(s.is_alive() for s in services if s.svc_id == "payment")
        # Torn-write probe runs OUT-OF-PROCESS: saga.py is imported inside a
        # child interpreter, so a poisoned coordinator can neither kill nor
        # hang the harness (crash -> None, hang -> timeout, both isolated;
        # services keep running parent-side and speak to the child through
        # the file broker).
        probe_payload = {
            "broker_root": str(broker_root),
            "services": list(SAGA_SERVICES),
            "wal_path": str(workspace_dir / "probe_wal.jsonl"),
            "node_id": COORD_ID,
            "timeout": 2.0,
            "seed": seed,
            "ops": [{"service": "payment", "item": "alpha", "qty": 1}],
        }
        result = _run_saga_probe_in_child(workspace_dir, "execute", probe_payload,
                                          site_dir, timeout=30.0)
        if result is None:
            data["notes"].append("torn probe crashed/hung (child isolated)")
            data["torn_probe_aborted"] = False
        else:
            data["torn_probe_aborted"] = not bool(result.get("ok"))
        try:
            import shutil as _shutil2

            # 在 Windows 平台下还原数据库文件时，必须先暂停/排他保护 payment 服务，防范 WinError 32 句柄锁定
            pay_svc = next((s for s in services if s.svc_id == "payment"), None)
            if pay_svc:
                with pay_svc._guard:
                    _shutil2.copyfile(backup, pay_db)
            else:
                _shutil2.copyfile(backup, pay_db)
        except OSError as exc:
            data["notes"].append("restore failed: %r" % (exc,))
        time.sleep(0.3)

        # Partition probe: isolate payment, expect abort; heal, expect success.
        write_partitions(broker_root, [("client", "payment"), (COORD_ID, "payment")])
        sender.matrix.partition([COORD_ID, "client"], ["payment"])
        result = _run_saga_probe_in_child(workspace_dir, "execute", probe_payload,
                                          site_dir, timeout=30.0)
        if result is None:
            data["notes"].append("partition probe crashed/hung (child isolated)")
        else:
            data["partition_probe_aborted"] = not bool(result.get("ok"))
        write_partitions(broker_root, [])
        sender.matrix.heal_all()
        time.sleep(0.5)
        result = _run_saga_probe_in_child(workspace_dir, "execute", probe_payload,
                                          site_dir, timeout=30.0)
        if result is None:
            data["notes"].append("healed probe crashed/hung (child isolated)")
        else:
            data["healed_probe_ok"] = bool(result.get("ok"))

        # Scripted deadlock: TA holds alpha wants beta; TB holds beta wants
        # alpha. The whole scenario (prepare_more race, waits polling,
        # resolve_deadlocks, victim rollback) runs OUT-OF-PROCESS inside one
        # child interpreter; the parent reads a single JSON verdict. A
        # poisoned saga.py can neither kill nor hang the harness.
        dl_payload = {
            "broker_root": str(broker_root),
            "services": list(SAGA_SERVICES),
            "wal_path": str(workspace_dir / "deadlock_wal.jsonl"),
            "node_id": COORD_ID,
            "timeout": 3.0,
            "seed": seed + 1,
            "wait_budget": 1.5,
            "join_budget": 6.0,
        }
        dl_result = _run_saga_probe_in_child(workspace_dir, "deadlock", dl_payload,
                                             site_dir, timeout=60.0)
        if dl_result is None:
            data["notes"].append("deadlock probe crashed/hung (child isolated)")
        else:
            for key in ("deadlock_base", "deadlock_cycle", "deadlock_pair"):
                if key in dl_result:
                    data[key] = dl_result[key]
            data["survivor_committed"] = bool(dl_result.get("survivor_committed"))
            data["victim_rolled_back"] = bool(dl_result.get("victim_rolled_back"))
            data["deadlock_threads_settled"] = bool(dl_result.get("threads_settled"))

        # Dangling-hold sweep (direct store read) + committed census.
        dangling = None
        try:
            total_holds = 0
            for svc in SAGA_SERVICES:
                con = sqlite3.connect(str(workspace_dir / ("%s.db" % svc)), timeout=5.0)
                try:
                    total_holds += con.execute("SELECT COUNT(*) FROM holds").fetchone()[0]
                finally:
                    con.close()
            dangling = total_holds
        except sqlite3.Error as exc:
            data["notes"].append("hold sweep failed: %r" % (exc,))
        data["dangling_holds"] = dangling

        txids: list[str] = []
        txn_services: dict[str, list[str]] = {}
        for block in (data.get("phase1a") or {}).get("txns", []):
            if block.get("ok"):
                txids.append(block["txid"])
                txn_services[block["txid"]] = sorted({op["service"] for op in block.get("ops", [])})
        for block in (data.get("phase2") or {}).get("txns", []):
            if block.get("ok"):
                txids.append(block["txid"])
                txn_services[block["txid"]] = sorted({op["service"] for op in block.get("ops", [])})
        if data.get("big_txid") and data.get("phase2"):
            txids.append(data["big_txid"])
            big_ops = ((data.get("phase1a") or {}).get("big") or {}).get("ops", [])
            txn_services[data["big_txid"]] = sorted({op["service"] for op in big_ops}) or list(SAGA_SERVICES)
        data["committed_txids"] = txids
        data["txn_services"] = txn_services
        if txids:
            data["service_states"] = _svc_statuses(broker_root, SAGA_SERVICES, txids)
        return data
    finally:
        try:
            mgr.shutdown(timeout=10.0)
        except Exception:
            pass
        for svc in services:
            svc.stop()
        for svc in services:
            svc.join(timeout=5.0)

# ---------------------------------------------------------------------------
# Milestone builders (10 milestones x 2 assertions per task = 40 total)
# ---------------------------------------------------------------------------

def build_raft_milestones(data: dict[str, Any]) -> list[MilestoneResult]:
    elections = data.get("elections", [])
    committed = data.get("committed", [])
    committed_recs = [{"k": r["k"], "v": r["v"]} for r in committed]
    post_heal = {
        nid: {"records": [{"k": k, "v": v} for k, v in sorted(store.items())]}
        for nid, store in (data.get("post_heal_stores") or {}).items()
    }
    final = {
        nid: {"records": [{"k": k, "v": v} for k, v in sorted(store.items())]}
        for nid, store in (data.get("final_stores") or {}).items()
    }
    single = InvariantChecker.check_single_leader(elections)
    persist = InvariantChecker.check_commit_persistence(committed_recs, final)
    consist_post = InvariantChecker.check_final_consistency(post_heal) if post_heal else None
    consist_final = InvariantChecker.check_final_consistency(final) if final else None
    linear = InvariantChecker.check_linearizability(data.get("client_ops", []))

    old_leader = data.get("leader")
    majority = [n for n in RAFT_NODE_IDS if n != old_leader]
    phase_a = data.get("phase_a_acked", [])
    union_post: set[str] = set()
    for store in (data.get("post_heal_stores") or {}).values():
        union_post.update(store.keys())
    old_store = (data.get("post_heal_stores") or {}).get(old_leader, {})
    new_store = (data.get("post_heal_stores") or {}).get(data.get("partition_leader") or "", {})

    return [
        _ms2("raft_cluster", 1, "Leader elected", [
            (data.get("leader") is not None, "leader=%r" % (data.get("leader"),)),
            (len(elections) > 0, "%d election records" % len(elections)),
        ]),
        _ms2("raft_cluster", 2, "Log replication (20 writes)", [
            (len(phase_a) == 20, "%d/20 acked" % len(phase_a)),
            (len(phase_a) > 0 and all(r["k"] in union_post for r in phase_a),
             "all acked keys present post-heal" if phase_a else "no acked writes to verify"),
        ]),
        _ms2("raft_cluster", 3, "Single leader per term", [
            (len(elections) > 0 and single.passed,
             single.detail if elections else "no elections observed"),
            (len({e.get("term") for e in elections if isinstance(e.get("term"), int)}) >= 1, "terms observed"),
        ]),
        _ms2("raft_cluster", 4, "Split-brain safety", [
            (len(phase_a) > 0 and data.get("minority_write_acked") is False,
             "minority write acked=%r" % (data.get("minority_write_acked"),) if phase_a else "no steady-state writes to verify partition"),
            (bool(union_post) and "minority_key" not in union_post,
             "minority key absent post-heal" if union_post else "no post-heal state to verify"),
        ]),
        _ms2("raft_cluster", 5, "Majority progress during partition", [
            (old_leader is not None and data.get("partition_leader") in majority,
             "partition leader=%r" % (data.get("partition_leader"),) if old_leader else "no initial leader to establish majority"),
            (len(data.get("partition_acked", [])) == 5,
             "%d/5 partition writes acked" % len(data.get("partition_acked", []))),
        ]),
        _ms2("raft_cluster", 6, "Heal catch-up convergence", [
            (bool(consist_post and consist_post.passed),
             consist_post.detail if consist_post else "no post-heal stores"),
            (bool(old_store) and old_store == new_store,
             "lagging node converged to majority state"),
        ]),
        _ms2("raft_cluster", 7, "Commit persistence", [
            (len(committed) > 0 and persist.passed,
             persist.detail if committed else "no committed records to verify"),
            (len(committed) >= 20, "%d committed records" % len(committed)),
        ]),
        _ms2("raft_cluster", 8, "Crash recovery (external SIGKILL)", [
            (data.get("kill_observed") is True, "marker kill observed"),
            (len(data.get("post_crash_acked", [])) > 0, "writes acked after restart"),
        ]),
        _ms2("raft_cluster", 9, "Restart convergence", [
            (data.get("final_leader") is not None,
             "final leader=%r" % (data.get("final_leader"),)),
            (bool(consist_final and consist_final.passed),
             consist_final.detail if consist_final else "no final stores"),
        ]),
        _ms2("raft_cluster", 10, "Client linearizability", [
            (len(data.get("client_ops", [])) > 0 and linear.passed,
             linear.detail if data.get("client_ops") else "no operations to verify"),
            (len(data.get("client_ops", [])) >= 20,
             "%d client ops recorded" % len(data.get("client_ops", []))),
        ]),
    ]


def build_saga_milestones(data: dict[str, Any]) -> list[MilestoneResult]:
    states: dict[str, dict[str, str]] = data.get("service_states") or {}
    committed: list[str] = data.get("committed_txids") or []
    txn_services: dict[str, list[str]] = data.get("txn_services") or {}
    committed_recs = [{"txid": t} for t in committed]
    node_states = {
        svc: {"records": [{"txid": t} for t in sorted(
            tx for tx, s in per.items() if s == "committed")]}
        for svc, per in states.items()
    }
    # "Never lost": every census txn survives on >=1 service (checker).
    persist = InvariantChecker.check_commit_persistence(
        committed_recs, node_states) if committed else None
    # Strict atomicity: every census txn is committed on ALL involved svcs.
    atomic_ok, atomic_detail = True, "all census txns atomic"
    for txid in committed:
        involved = txn_services.get(txid) or list(SAGA_SERVICES)
        got = {svc: states.get(svc, {}).get(txid, "?") for svc in involved}
        if not all(s == "committed" for s in got.values()):
            atomic_ok = False
            atomic_detail = "non-atomic %s: %r" % (txid, got)
            break

    phase1a = data.get("phase1a") or {}
    phase2 = data.get("phase2") or {}
    p1_ok = [t.get("ok") for t in phase1a.get("txns", [])]
    p2_ok = [t.get("ok") for t in (phase2.get("txns") or [])]
    p1_ids = [t.get("txid") for t in phase1a.get("txns", []) if t.get("ok")]
    p2_ids = [t.get("txid") for t in (phase2.get("txns") or []) if t.get("ok")]

    def _all_committed(txids: list[str]) -> bool:
        # Committed on every *involved* service (subset participation is valid).
        for t in txids:
            involved = txn_services.get(t, list(SAGA_SERVICES))
            if not involved:
                return False
            for svc in involved:
                if states.get(svc, {}).get(t) != "committed":
                    return False
        return bool(txids)

    big = data.get("big_txid")
    big_states = {svc: (states.get(svc, {}).get(big, "?") if big else "?") for svc in SAGA_SERVICES}
    big_resolved = big is not None and (
        all(s == "committed" for s in big_states.values())
        or (all(s in ("aborted", "unknown", "?") for s in big_states.values())
            and "prepared" not in big_states.values()))
    cycle = data.get("deadlock_cycle") or []

    return [
        _ms2("saga_coordinator", 1, "Unanimous prepare (steady state)", [
            (len(p1_ok) == 3 and all(p1_ok), "phase1 prepares=%r" % (p1_ok,)),
            (len(p2_ok) == 2 and all(p2_ok), "phase2 prepares=%r" % (p2_ok,)),
        ]),
        _ms2("saga_coordinator", 2, "Atomic commit on all services", [
            (_all_committed(p1_ids), "phase1 txns committed everywhere"),
            (_all_committed(p2_ids), "phase2 txns committed everywhere"),
        ]),
        _ms2("saga_coordinator", 3, "Marker-triggered external SIGKILL", [
            (data.get("kill_observed") is True, "kill at commit point observed"),
            (bool(data.get("nemesis_log")) and data.get("nemesis_ok") is True,
             "nemesis events ok=%r" % (data.get("nemesis_log"),) if data.get("nemesis_log") else "no nemesis events executed"),
        ]),
        _ms2("saga_coordinator", 4, "Crash recovery without dangling prepares", [
            (big is not None and big_resolved, "big txn states=%r" % (big_states,) if big else "no big txn created to recover"),
            (big is not None and phase2 is not None and isinstance(phase2.get("recovered"), list) and (
                big in phase2["recovered"]
            ), "recover() ran on restart and restored big_txid" if big else "no big txn created to verify recovery"),
        ]),
        _ms2("saga_coordinator", 5, "Torn-write fail-closed", [
            (data.get("svc_alive_after_corrupt") is True, "payment service survived corrupt_file"),
            (data.get("torn_probe_aborted") is True, "affected txn aborted safely"),
        ]),
        _ms2("saga_coordinator", 6, "Partition timeout abort + heal progress", [
            (data.get("partition_probe_aborted") is True, "isolated txn aborted (no hang)"),
            (data.get("healed_probe_ok") is True, "post-heal txn committed"),
        ]),
        _ms2("saga_coordinator", 7, "Deadlock detected", [
            (len(cycle) > 0, "cycle=%r" % (cycle,)),
            (len(set(cycle)) >= 2, "cycle spans >=2 transactions"),
        ]),
        _ms2("saga_coordinator", 8, "Deadlock resolved with progress", [
            (data.get("survivor_committed") is True, "exactly one survivor committed"),
            (data.get("victim_rolled_back") is True, "victim rolled back"),
        ]),
        _ms2("saga_coordinator", 9, "Idempotent recovery, no dangling holds", [
            (bool(phase2) and isinstance(phase2.get("recovered"), list) and len(phase2.get("recovered", [])) > 0 and (phase2.get("recovered_again") == []),
             "second recover() is a no-op" if (phase2 and phase2.get("recovered")) else "no initial recoveries to verify idempotence"),
            (bool(phase2) and data.get("dangling_holds") == 0 and len(committed) > 0,
             "holds left=%r" % (data.get("dangling_holds"),) if committed else "no committed txns to verify clean state"),
        ]),
        _ms2("saga_coordinator", 10, "Atomicity + never-lost persistence", [
            (len(committed) > 0 and atomic_ok,
             atomic_detail if committed else "no committed txns to verify"),
            (bool(persist and persist.passed), persist.detail if persist else "no committed census"),
        ]),
    ]


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

RAFT_CONTRACT = """\
# Task raft_cluster — 3-node Raft consensus (harness contract)

Write `raft.py` (stdlib only): `python raft.py <node_id> <broker_root> <workdir> <seed>`.
Implement leader election, heartbeat log replication, majority commit, and
restart recovery honouring this contract:

* Mailboxes: `<broker_root>/mailbox_<id>/msg-*.json`, envelope
  `{seq, src, dst, send_time, deliver_at, payload}` (atomic tmp+replace writes;
  delete on delivery; skip corrupt files). Node ids: n1, n2, n3, client.
* Partitions: re-read `<broker_root>/partitions.json` (`{"cuts": [[a,b]]}`,
  bidirectional) at >=20Hz; never send to / never accept from cut peers.
* Messages: RequestVote/Vote/Append/Ack/client_write/client_read/
  client_status/bye with the field shapes of the reference protocol
  (term, candidate, last_idx/last_term, prev_idx/prev_term, entries,
  leader_commit, match_idx, req_id/key/value). Every client_* reply MUST
  echo the request's req_id (unmatched replies are dropped). Two reply
  payloads are consumed by the harness verbatim:
  - `client_status_reply` MUST carry `role` (the string `"leader"` while
    this node believes it leads) and `term` (int);
  - `client_write_reply` MUST carry `ok` (bool) — the harness acks a write
    only when this is true;
  - `client_read_reply` MUST carry `value`.
* Durability: `state.json` (term, voted_for, log, commit_index, store,
  results) atomically persisted before every client ack.
* Observability: append `{"term", "leader"}` to `elections_<id>.log` on
  every new-term leadership observation; print `CRITICAL_WRITE_POINT`
  (flushed) immediately before persisting a client-bearing commit.
* Liveness: heartbeats ~50ms, election timeout ~180-330ms randomised,
  client-write ack only after majority commit; minority writes must time
  out unacked (never dual-commit across a split brain).
"""

SAGA_CONTRACT = """\
# Task saga_coordinator — Saga over 3 SQLite services (harness contract)

Write `saga.py` (stdlib only) with `class Coordinator(broker_root, services,
wal_path, node_id="coord", timeout=2.5, seed=0)` driving the services
`inventory/payment/shipping` over `<broker_root>/mailbox_<id>/` with
`partitions.json` cuts honoured on every send/recv:

* Mailboxes: `<broker_root>/mailbox_<id>/msg-*.json` — the file name MUST
  start with `msg-` and end with `.json`; files named anything else are not
  delivered. Each file holds one envelope `{seq, src, dst, send_time,
  deliver_at, payload}` where `payload` is the protocol message below;
  write atomically (temp file + `os.replace`), delete a file once consumed,
  and skip corrupt files. Service ids are `inventory`, `payment`,
  `shipping`; your own mailbox id is the `node_id` you were constructed with.
* Partitions: re-read `<broker_root>/partitions.json` (`{"cuts": [[a, b]]}`,
  bidirectional) frequently; never send to / never accept from cut peers.
* op: `{"service", "item", "qty"}`. Service protocol: prepare/commit/
  rollback/status/waits with `{type, txid, item, qty, req_id}` and
  `{type: <kind>_ack, req_id, ok, ...}` replies (match by req_id).
  Two reply shapes are consumed by the harness verbatim:
  - `status_ack` MUST carry `state` (the txid's per-service state string);
  - `waits_ack` MUST carry `holders` (item -> txid holding it) and
    `waiters` (txid -> item it is blocked on) for the deadlock resolver.
* `begin(ops)->txid`, `prepare(txid)->{svc: bool}`,
  `prepare_more(txid, extra)->{svc: bool}`, `commit(txid, hold_open=0.0)`,
  `rollback(txid, reason="")`, `execute(ops)->(ok, txid)`.
* Durability: JSONL WAL (begin/prepared/commit-start/committed/aborted,
  fsync-appended); `commit` prints `CRITICAL_WRITE_POINT` (flushed) after
  the commit-start WAL append; `recover()->[txids]` resolves dangling
  prepares (commit iff every participant still holds prepared/committed)
  and is idempotent.
* `detect_deadlock(graph)->cycle|None` (static, DFS) and
  `resolve_deadlocks()` (query `waits`, abort the youngest cycle member).
* Timeouts everywhere (prepare/commit <= timeout, 3x commit retry);
  never hang on an isolated participant — abort instead.
"""

LONG_BRIEFS = {
    "raft_cluster": ("Three-node Raft with split-brain + SIGKILL recovery", RAFT_CONTRACT),
    "saga_coordinator": ("Saga 2PC over inventory/payment/shipping + deadlock", SAGA_CONTRACT),
}


class LongTaskSuite(SuiteAdapter):
    """Distributed consensus + transaction tasks under Jepsen chaos."""

    suite_name = "long"
    TASK_IDS = ("raft_cluster", "saga_coordinator")

    def describe_task(self, task_id: str) -> dict[str, Any]:
        title, _ = LONG_BRIEFS[task_id]
        return {"task_id": task_id, "title": title}

    def prepare_task(self, task_id: str, workspace_dir: Path) -> None:
        title, contract = LONG_BRIEFS[task_id]
        (workspace_dir / "TASK.md").write_text(
            "# %s\n\n%s\n" % (title, contract), encoding="utf-8")

    def build_prompt(self, task_id: str, workspace_dir: Path) -> str:
        title, contract = LONG_BRIEFS[task_id]
        target = "raft.py" if task_id == "raft_cluster" else "saga.py"
        return (
            "You are implementing a distributed-systems benchmark task: %s.\n\n"
            "%s\n\nRules: use the `write` or `edit` tool to create ONLY `%s` "
            "in the workspace (stdlib only, no network) — bash/`python -c` "
            "prototypes are not scored; honour every line of the contract "
            "above, including the partitions file, the marker line and the "
            "durability rules. When the file on disk is ready, call the "
            "`finish` tool with a non-empty summary of what you changed and "
            "how you checked it; a reply with no tool calls does not end "
            "the task." % (title, contract, target)
        )

    def evaluate_task(
        self,
        task_id: str,
        workspace_dir: Path,
        trajectory: AgentTrajectory,  # noqa: ARG002 - hook signature
    ) -> tuple[list[MilestoneResult], dict[str, Any]]:
        if task_id == "raft_cluster":
            data = run_raft_scenario(workspace_dir)
            return build_raft_milestones(data), {}
        data = run_saga_scenario(workspace_dir)
        return build_saga_milestones(data), {}


# ---------------------------------------------------------------------------
# Unit self-tests (reference impls must pass 40/40; broken ones must fail)
# ---------------------------------------------------------------------------

BROKEN_SAGA = REFERENCE_SAGA.replace(
    "    def prepare(self, txid):\n"
    "        results = self._prepare_ops(txid, self._txns.get(txid, []))\n",
    "    def prepare(self, txid):\n"
    "        results = {op[\"service\"]: False for op in self._txns.get(txid, [])}\n"
    "        _ = self._prepare_ops\n",
)

DEAD_RAFT = "import sys, time\nwhile True:\n    time.sleep(1)\n"


def _count_subassertions(milestones: list[MilestoneResult]) -> int:
    total = 0
    for m in milestones:
        total += m.diagnostics.count("(PASS") + m.diagnostics.count("(FAIL")
    return total


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} long::{name}", flush=True)

    suite = LongTaskSuite()
    check("task_ids", suite.task_ids() == ["raft_cluster", "saga_coordinator"])

    # -- reference Raft: 10/10 milestones, 20 sub-assertions --
    with tempfile.TemporaryDirectory(prefix="long-raft-") as tmp:
        ws = Path(tmp)
        (ws / "raft.py").write_text(REFERENCE_RAFT, encoding="utf-8")
        data = run_raft_scenario(ws)
        milestones = build_raft_milestones(data)
        n_pass = sum(1 for m in milestones if m.passed)
        check("raft_ref_%d_of_10" % n_pass, n_pass == 10)
        check("raft_20_assertions", _count_subassertions(milestones) == 20)
        for m in milestones:
            if not m.passed:
                print("    raft FAIL %s: %s" % (m.milestone_id, m.diagnostics), flush=True)

    # -- missing raft.py fails fast without spawning anything --
    with tempfile.TemporaryDirectory(prefix="long-missing-") as tmp:
        data = run_raft_scenario(Path(tmp))
        check("raft_missing_no_leader", data["leader"] is None)
        milestones = build_raft_milestones(data)
        check("raft_missing_all_fail", all(not m.passed for m in milestones))

    # -- dead raft (never elects) fails on a short budget --
    with tempfile.TemporaryDirectory(prefix="long-dead-") as tmp:
        ws = Path(tmp)
        (ws / "raft.py").write_text(DEAD_RAFT, encoding="utf-8")
        data = run_raft_scenario(ws, leader_timeout=3.0)
        check("raft_dead_no_leader", data["leader"] is None)

    # -- reference Saga: 10/10 milestones, 20 sub-assertions --
    with tempfile.TemporaryDirectory(prefix="long-saga-") as tmp:
        ws = Path(tmp)
        (ws / "saga.py").write_text(REFERENCE_SAGA, encoding="utf-8")
        data = run_saga_scenario(ws)
        milestones = build_saga_milestones(data)
        n_pass = sum(1 for m in milestones if m.passed)
        check("saga_ref_%d_of_10" % n_pass, n_pass == 10)
        check("saga_20_assertions", _count_subassertions(milestones) == 20)
        for m in milestones:
            if not m.passed:
                print("    saga FAIL %s: %s" % (m.milestone_id, m.diagnostics), flush=True)

    # -- broken Saga (prepares always fail) fails steady-state milestones --
    with tempfile.TemporaryDirectory(prefix="long-broken-") as tmp:
        ws = Path(tmp)
        (ws / "saga.py").write_text(BROKEN_SAGA, encoding="utf-8")
        data = run_saga_scenario(ws)
        milestones = build_saga_milestones(data)
        by_id = {m.milestone_id: m for m in milestones}
        check("saga_broken_prepare_fails", not by_id["saga_coordinator_m01"].passed)

    check("suite_assertion_total_40", True)  # 20 raft + 20 saga (see above)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"long_task self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
