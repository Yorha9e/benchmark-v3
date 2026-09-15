"""File-mailbox message bus with partition matrix and fault injection.

Covers ``benchmark_v3/bench_harness/jepsen/broker.py``:

* :class:`PartitionMatrix` — directed reachability between simulated nodes.
  Supports bidirectional cuts (:meth:`partition`), isolating a group from the
  rest (:meth:`isolate_nodes`), split-brain layouts (:meth:`split_brain`),
  asymmetric one-way cuts (:meth:`cut_directed`), and dynamic healing
  (:meth:`heal_partition`, :meth:`heal_all`).
* :class:`FileMessageBroker` — file-based mailbox bus. Every node owns
  ``<root>/mailbox_<node_id>/``; each message is one JSON file written
  atomically (temp file + ``os.replace``). Fault injection modes:

  - packet drop (loss rate 0.0–1.0, global or per-sender override),
  - delay / latency injection (base seconds + uniform jitter),
  - message reordering (deterministic shuffle on receive) and duplicate
    injection (duplicate rate 0.0–1.0),
  - dynamic partition updates delegated to the :class:`PartitionMatrix`.

All randomness flows through a seeded ``random.Random`` instance, so a run is
exactly reproducible given the same seed and the same call sequence.

Standard library only, cross-platform (Windows + POSIX).
"""

from __future__ import annotations

import itertools
import json
import os
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

__all__ = ["FileMessageBroker", "PartitionMatrix"]


def _as_list(nodes: str | Iterable[str]) -> list[str]:
    if isinstance(nodes, str):
        return [nodes]
    return list(nodes)


class PartitionMatrix:
    """Directed network-partition matrix over a fixed node set.

    Absence of a cut means delivery is allowed; ``can_send(a, b)`` is False
    only when a directed cut ``(a, b)`` exists. A node can always send to
    itself (loopback is never partitioned).
    """

    def __init__(self, node_ids: Iterable[str]) -> None:
        self._nodes = list(dict.fromkeys(node_ids))  # de-dup, keep order
        if not self._nodes:
            raise ValueError("PartitionMatrix needs at least one node")
        self._cut: set[tuple[str, str]] = set()

    # -- introspection ----------------------------------------------------
    @property
    def node_ids(self) -> list[str]:
        return list(self._nodes)

    def _check(self, *nodes: str) -> None:
        for node in nodes:
            if node not in self._nodes:
                raise KeyError(f"unknown node: {node!r}")

    def can_send(self, src: str, dst: str) -> bool:
        """Return True if a message from *src* may currently reach *dst*."""
        self._check(src, dst)
        if src == dst:
            return True
        return (src, dst) not in self._cut

    def cuts(self) -> frozenset[tuple[str, str]]:
        """Snapshot of the current directed cuts."""
        return frozenset(self._cut)

    def groups(self) -> list[set[str]]:
        """Connected components over currently allowed (undirected) links."""
        parent = {n: n for n in self._nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for a in self._nodes:
            for b in self._nodes:
                if a != b and (a, b) not in self._cut:
                    union(a, b)
        buckets: dict[str, set[str]] = {}
        for node in self._nodes:
            buckets.setdefault(find(node), set()).add(node)
        return sorted(buckets.values(), key=lambda g: sorted(g))

    # -- fault operations -------------------------------------------------
    def partition(
        self, nodes_a: str | Iterable[str], nodes_b: str | Iterable[str]
    ) -> None:
        """Bidirectionally cut every link between set A and set B."""
        group_a, group_b = _as_list(nodes_a), _as_list(nodes_b)
        self._check(*group_a, *group_b)
        for a in group_a:
            for b in group_b:
                if a != b:
                    self._cut.add((a, b))
                    self._cut.add((b, a))

    def isolate_nodes(self, group: str | Iterable[str]) -> None:
        """Isolate *group* from every node outside it (bidirectional)."""
        members = _as_list(group)
        self._check(*members)
        rest = [n for n in self._nodes if n not in members]
        self.partition(members, rest)

    def split_brain(
        self,
        part_a: str | Iterable[str],
        part_b: str | Iterable[str] | None = None,
    ) -> None:
        """Split-brain: cut between *part_a* and *part_b* (default: rest)."""
        group_a = _as_list(part_a)
        group_b = _as_list(part_b) if part_b is not None else [
            n for n in self._nodes if n not in group_a
        ]
        self.partition(group_a, group_b)

    def cut_directed(self, src: str, dst: str) -> None:
        """Asymmetric cut: drop *src* -> *dst* while *dst* -> *src* survives."""
        self._check(src, dst)
        if src != dst:
            self._cut.add((src, dst))

    def heal_directed(self, src: str, dst: str) -> None:
        """Heal one directed edge."""
        self._check(src, dst)
        self._cut.discard((src, dst))

    def heal_partition(
        self,
        nodes_a: str | Iterable[str] | None = None,
        nodes_b: str | Iterable[str] | None = None,
    ) -> None:
        """Heal partition damage.

        * both sets given: heal links between them (both directions);
        * one set given: heal every cut touching that set;
        * neither given: heal everything (same as :meth:`heal_all`).
        """
        if nodes_a is None and nodes_b is None:
            self.heal_all()
            return
        group_a = _as_list(nodes_a) if nodes_a is not None else None
        group_b = _as_list(nodes_b) if nodes_b is not None else None
        if group_a is not None:
            self._check(*group_a)
        if group_b is not None:
            self._check(*group_b)
        if group_a is not None and group_b is not None:
            for a in group_a:
                for b in group_b:
                    self._cut.discard((a, b))
                    self._cut.discard((b, a))
        else:
            touched = set(group_a if group_a is not None else group_b or [])
            self._cut = {
                (s, d) for (s, d) in self._cut if s not in touched and d not in touched
            }

    def heal_all(self) -> None:
        """Remove every cut; restore full connectivity."""
        self._cut.clear()


class FileMessageBroker:
    """File-backed mailbox bus between simulated nodes with fault injection.

    Parameters
    ----------
    root:
        Directory holding ``mailbox_<node_id>/`` subdirectories (created).
    node_ids:
        Participating node ids (also seed the :class:`PartitionMatrix`).
    loss_rate / duplicate_rate:
        Probabilities in ``[0.0, 1.0]`` applied per :meth:`send`.
    base_delay / jitter:
        Delivery delay in seconds: ``base_delay + uniform(0, jitter)``.
    reorder:
        When True, due messages are shuffled deterministically on receive.
    seed:
        RNG seed for reproducibility.
    """

    _GLOB = "msg_*.json"

    def __init__(
        self,
        root: str | os.PathLike[str],
        node_ids: Iterable[str],
        loss_rate: float = 0.0,
        base_delay: float = 0.0,
        jitter: float = 0.0,
        duplicate_rate: float = 0.0,
        reorder: bool = False,
        seed: int = 0,
    ) -> None:
        self.root = str(root)
        self.matrix = PartitionMatrix(node_ids)
        self.set_loss_rate(loss_rate)
        self.set_latency(base_delay, jitter)
        self.set_duplicate_rate(duplicate_rate)
        self.reorder = bool(reorder)
        self._rng = random.Random(seed)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._node_latency: dict[str, float] = {}
        self._node_loss: dict[str, float] = {}
        self._stats = {
            "sent": 0,
            "delivered": 0,
            "dropped_partition": 0,
            "dropped_loss": 0,
            "delayed": 0,
            "duplicated": 0,
            "corrupt_skipped": 0,
        }
        Path(self.root).mkdir(parents=True, exist_ok=True)
        for node_id in self.matrix.node_ids:
            self._mailbox(node_id).mkdir(parents=True, exist_ok=True)

    # -- configuration ----------------------------------------------------
    @staticmethod
    def _check_rate(value: float, name: str) -> float:
        rate = float(value)
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"{name} must be in [0.0, 1.0], got {value!r}")
        return rate

    def set_loss_rate(self, rate: float) -> None:
        """Set the global packet-loss probability."""
        self._loss_rate = self._check_rate(rate, "loss_rate")

    def set_duplicate_rate(self, rate: float) -> None:
        """Set the global duplicate-injection probability."""
        self._duplicate_rate = self._check_rate(rate, "duplicate_rate")

    def set_latency(self, base_delay: float, jitter: float = 0.0) -> None:
        """Set global latency: ``base_delay + uniform(0, jitter)`` seconds."""
        if base_delay < 0 or jitter < 0:
            raise ValueError("delays must be >= 0")
        self._base_delay = float(base_delay)
        self._jitter = float(jitter)

    def set_reorder(self, enabled: bool) -> None:
        """Enable/disable deterministic receive-time reordering."""
        self.reorder = bool(enabled)

    def set_node_latency(self, node_id: str, seconds: float) -> None:
        """Extra send latency (seconds) for messages from *node_id*."""
        self.matrix._check(node_id)
        if seconds < 0:
            raise ValueError("node latency must be >= 0")
        self._node_latency[node_id] = float(seconds)

    def clear_node_latency(self, node_id: str) -> None:
        self._node_latency.pop(node_id, None)

    def set_node_loss(self, node_id: str, rate: float) -> None:
        """Per-sender loss override for *node_id*."""
        self.matrix._check(node_id)
        self._node_loss[node_id] = self._check_rate(rate, "loss rate")

    def clear_node_loss(self, node_id: str) -> None:
        self._node_loss.pop(node_id, None)

    # -- messaging --------------------------------------------------------
    def _mailbox(self, node_id: str) -> Path:
        return Path(self.root) / f"mailbox_{node_id}"

    def send(self, src: str, dst: str, payload: Any) -> str | None:
        """Send *payload* from *src* to *dst*.

        Returns the message id, or None when the message is dropped by a
        partition or by packet loss. Raises ``KeyError`` for unknown nodes
        and ``TypeError`` for non-JSON-serializable payloads.
        """
        self.matrix._check(src, dst)
        try:
            body = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"payload is not JSON-serializable: {exc}") from exc
        with self._lock:
            self._stats["sent"] += 1
            if not self.matrix.can_send(src, dst):
                self._stats["dropped_partition"] += 1
                return None
            loss = self._node_loss.get(src, self._loss_rate)
            if self._rng.random() < loss:
                self._stats["dropped_loss"] += 1
                return None
            delay = (
                self._base_delay
                + self._rng.uniform(0.0, self._jitter)
                + self._node_latency.get(src, 0.0)
            )
            now = time.time()
            seq = next(self._seq)
            envelope = {
                "seq": seq,
                "src": src,
                "dst": dst,
                "send_time": now,
                "deliver_at": now + delay,
                "payload": json.loads(body),  # normalized round-trip
            }
            if delay > 0:
                self._stats["delayed"] += 1
            msg_id = self._store(dst, f"msg_{seq:010d}.json", envelope)
            if self._rng.random() < self._duplicate_rate:
                self._store(dst, f"msg_{seq:010d}__dup.json", envelope)
                self._stats["duplicated"] += 1
            return msg_id

    def _store(self, dst: str, filename: str, envelope: dict[str, Any]) -> str:
        box = self._mailbox(dst)
        box.mkdir(parents=True, exist_ok=True)
        tmp = box / f".tmp_{uuid.uuid4().hex}.json"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh)
        os.replace(tmp, box / filename)
        return Path(filename).stem

    def recv(
        self,
        node_id: str,
        max_messages: int | None = None,
        include_future: bool = False,
    ) -> list[tuple[str, Any]]:
        """Collect due messages for *node_id* as ``[(src, payload)]``.

        Delivery removes the backing files. Messages whose ``deliver_at``
        lies in the future stay queued unless *include_future* is set.
        """
        self.matrix._check(node_id)
        now = time.time()
        due: list[tuple[float, int, str, Any]] = []
        with self._lock:
            box = self._mailbox(node_id)
            paths = sorted(box.glob(self._GLOB))
            for path in paths:
                try:
                    with open(path, encoding="utf-8") as fh:
                        envelope = json.load(fh)
                    deliver_at = float(envelope["deliver_at"])
                    seq = int(envelope["seq"])
                except (OSError, ValueError, KeyError):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._stats["corrupt_skipped"] += 1
                    continue
                if deliver_at <= now or include_future:
                    due.append(
                        (deliver_at, seq, str(envelope["src"]), envelope["payload"])
                    )
                # else: not yet deliverable; leave the file queued.
            due.sort(key=lambda item: (item[0], item[1]))
            if self.reorder and len(due) > 1:
                self._rng.shuffle(due)
            if max_messages is not None:
                due = due[: max(0, max_messages)]
            wanted = {seq for (_, seq, _, _) in due}
            # Remove delivered files (both originals and duplicates of the
            # same seq so a duplicate is delivered exactly as queued).
            delivered = 0
            for path in paths:
                try:
                    with open(path, encoding="utf-8") as fh:
                        seq = int(json.load(fh)["seq"])
                except (OSError, ValueError, KeyError):
                    continue
                if seq in wanted:
                    try:
                        path.unlink()
                        delivered += 1
                    except OSError:
                        pass
            # Count logical messages delivered (dedup by seq).
            self._stats["delivered"] += len(due)
        return [(src, payload) for (_, _, src, payload) in due]

    def pending_count(self, node_id: str) -> int:
        """Number of queued (possibly future-dated) files for *node_id*."""
        self.matrix._check(node_id)
        return len(list(self._mailbox(node_id).glob(self._GLOB)))

    def clear(self, node_id: str | None = None) -> int:
        """Drop all queued files (one mailbox, or every mailbox)."""
        targets = (
            [node_id] if node_id is not None else list(self.matrix.node_ids)
        )
        removed = 0
        with self._lock:
            for target in targets:
                self.matrix._check(target)
                for path in self._mailbox(target).glob(self._GLOB):
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    # -- telemetry ----------------------------------------------------------
    def stats(self) -> dict[str, int | float]:
        """Snapshot of send/deliver/drop counters plus fault settings."""
        with self._lock:
            snapshot: dict[str, int | float] = dict(self._stats)
        snapshot.update(
            {
                "loss_rate": self._loss_rate,
                "base_delay": self._base_delay,
                "jitter": self._jitter,
                "duplicate_rate": self._duplicate_rate,
                "reorder": self.reorder,
            }
        )
        return snapshot

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0


# ---------------------------------------------------------------------------
# Unit self-tests (stdlib only; safe on Windows + POSIX, no network).
# ---------------------------------------------------------------------------


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} broker::{name}", flush=True)

    # --- PartitionMatrix ---
    matrix = PartitionMatrix(["n1", "n2", "n3"])
    check("mesh_open", matrix.can_send("n1", "n3"))
    check("loopback", matrix.can_send("n1", "n1"))
    matrix.isolate_nodes(["n3"])
    check("isolate_blocks", not matrix.can_send("n1", "n3"))
    check("isolate_blocks_reverse", not matrix.can_send("n3", "n1"))
    check("isolate_keeps_inside", matrix.can_send("n1", "n2"))
    check("groups_split", len(matrix.groups()) == 2)
    matrix.heal_all()
    check("heal_all", matrix.can_send("n1", "n3") and len(matrix.groups()) == 1)

    matrix.partition(["n1"], ["n2"])
    check("partition_bidirectional", not matrix.can_send("n1", "n2") and not matrix.can_send("n2", "n1"))
    check("partition_spares_third", matrix.can_send("n1", "n3"))
    matrix.heal_partition(["n1"], ["n2"])
    check("heal_partition_pair", matrix.can_send("n1", "n2"))

    matrix.split_brain(["n1", "n2"])
    check("split_brain", not matrix.can_send("n1", "n3") and matrix.can_send("n1", "n2"))
    matrix.heal_partition()
    check("heal_partition_empty", matrix.can_send("n1", "n3"))

    matrix.cut_directed("n1", "n2")
    check(
        "asymmetric",
        not matrix.can_send("n1", "n2") and matrix.can_send("n2", "n1"),
    )
    matrix.heal_directed("n1", "n2")
    check("heal_directed", matrix.can_send("n1", "n2"))

    try:
        PartitionMatrix([])
        check("empty_rejected", False)
    except ValueError:
        check("empty_rejected", True)
    try:
        matrix.can_send("n1", "ghost")
        check("unknown_rejected", False)
    except KeyError:
        check("unknown_rejected", True)

    # --- FileMessageBroker ---
    with tempfile.TemporaryDirectory(prefix="jepsen-broker-") as tmp:
        broker = FileMessageBroker(tmp, ["n1", "n2", "n3"], seed=1234)
        mid = broker.send("n1", "n2", {"op": "write", "k": 1})
        check("send_id", isinstance(mid, str) and len(mid) > 0)
        got = broker.recv("n2")
        check("roundtrip", got == [("n1", {"op": "write", "k": 1})])
        check("mailbox_drained", broker.pending_count("n2") == 0)

        # Partition drops at send time.
        broker.matrix.isolate_nodes(["n3"])
        check("partition_drop", broker.send("n1", "n3", "x") is None)
        check("partition_no_queue", broker.pending_count("n3") == 0)
        broker.matrix.heal_all()
        check("heal_resumes", broker.recv("n3") == [] and broker.send("n1", "n3", "y") is not None)

        # Total loss.
        broker.set_loss_rate(1.0)
        check("loss_drop", broker.send("n1", "n2", "x") is None)
        broker.set_loss_rate(0.0)
        check("loss_cleared", broker.send("n1", "n2", "x") is not None)
        broker.clear("n2")

        # Latency: queued now, deliverable after the delay.
        broker.set_latency(0.05, 0.0)
        broker.send("n1", "n2", "slow")
        check("delay_holds", broker.recv("n2") == [])
        deadline = time.monotonic() + 5.0
        delivered: list[tuple[str, Any]] = []
        while time.monotonic() < deadline and not delivered:
            time.sleep(0.02)
            delivered = broker.recv("n2")
        check("delay_delivers", delivered == [("n1", "slow")])
        broker.set_latency(0.0, 0.0)

        # Duplicates.
        broker.set_duplicate_rate(1.0)
        broker.send("n1", "n2", "dup")
        check("duplicate", broker.recv("n2") == [("n1", "dup"), ("n1", "dup")])
        broker.set_duplicate_rate(0.0)

        # Reorder is deterministic for a fixed seed + script.
        # Rebuild explicitly for a clean determinism comparison.
        orders = []
        for _ in range(2):
            rb = FileMessageBroker(tmp, ["a", "b"], reorder=True, seed=7)
            rb.clear("b")
            for i in range(10):
                rb.send("a", "b", i)
            orders.append([v for _, v in rb.recv("b")])
        check("reorder_deterministic", orders[0] == orders[1])
        check("reorder_complete", sorted(orders[0]) == list(range(10)))

        # Stats + validation.
        stats = broker.stats()
        check("stats_keys", all(k in stats for k in ("sent", "delivered", "dropped_partition", "dropped_loss")))
        try:
            broker.set_loss_rate(1.5)
            check("rate_validated", False)
        except ValueError:
            check("rate_validated", True)
        try:
            broker.send("n1", "n2", object())
            check("payload_validated", False)
        except TypeError:
            check("payload_validated", True)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"broker self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
