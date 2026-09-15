"""Declarative chaos-timeline runner (the "nemesis").

Covers ``benchmark_v3/bench_harness/jepsen/nemesis.py``:

* :class:`TimelineEvent` — one chaos step:
  ``(timestamp_offset, action_type, target, params)``.
* :class:`ScenarioTimeline` — validated, time-ordered event list with
  ``from_dicts`` / ``from_tuples`` constructors.
* :class:`ChaosNemesis` — executes a timeline deterministically during a test
  run, either blocking (:meth:`run`) or in the background
  (:meth:`start_background` / context-manager protocol), recording an event
  log of what fired, when, and with what outcome.

Built-in actions (all delegate to a bound supervisor / broker so this module
never imports its siblings — duck typing only):

================= ============ ===============================================
action            target       params
================= ============ ===============================================
``partition``     nodes_a      ``{"nodes_b": [...]}``
``heal``          — / nodes_a  ``{"nodes_b": [...]}`` (empty heals all)
``kill``          node_id      ``{"hard": True}``
``restart``       node_id      ``{"graceful": False}``
``corrupt_file``  path         ``{"mode": "truncate"|"bitflip"|"enospc", ...}``
``delay``         node_id      ``{"ms": <int>}`` (broker latency injection)
``set_loss``      — / node_id  ``{"rate": 0.0-1.0}``
``set_latency``   — / node_id  ``{"base": s, "jitter": s}``
================= ============ ===============================================

Custom actions can be added with :meth:`ChaosNemesis.register_handler`.

Standard library only, cross-platform (Windows + POSIX).
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "CORRUPT_MODES",
    "ChaosNemesis",
    "EventRecord",
    "ScenarioTimeline",
    "TimelineEvent",
    "VALID_ACTIONS",
]

#: Actions understood out of the box (see module table for target/params).
VALID_ACTIONS = (
    "partition",
    "heal",
    "kill",
    "restart",
    "corrupt_file",
    "delay",
    "set_loss",
    "set_latency",
)

#: Supported ``corrupt_file`` modes.
CORRUPT_MODES = ("truncate", "bitflip", "enospc")


@dataclass(frozen=True)
class TimelineEvent:
    """One chaos step: fire *action* on *target* at ``offset`` seconds."""

    offset: float
    action: str
    target: Any = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError(f"event offset must be >= 0, got {self.offset!r}")
        if self.action not in VALID_ACTIONS:
            raise ValueError(
                f"unknown action {self.action!r}; expected one of {VALID_ACTIONS}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "action": self.action,
            "target": self.target,
            "params": dict(self.params),
        }


@dataclass
class EventRecord:
    """What actually happened when a :class:`TimelineEvent` fired."""

    offset: float
    action: str
    target: Any
    params: dict[str, Any]
    executed_at: float  # wall-clock time of execution
    lateness: float  # executed_at - (start_wall + offset), seconds
    ok: bool
    error: str = ""


class ScenarioTimeline:
    """Validated, time-ordered list of :class:`TimelineEvent`."""

    def __init__(self, events: Iterable[TimelineEvent]) -> None:
        items = list(events)
        for item in items:
            if not isinstance(item, TimelineEvent):
                raise TypeError(f"expected TimelineEvent, got {type(item).__name__}")
        # Stable sort: same-offset events keep declaration order.
        self._events = sorted(items, key=lambda e: e.offset)

    @classmethod
    def from_dicts(cls, rows: Iterable[Mapping[str, Any]]) -> ScenarioTimeline:
        """Build from ``[{"offset":..,"action":..,"target":..,"params":{}}]``.

        Key aliases: ``at`` for ``offset``, ``do`` for ``action``.
        """
        events = []
        for row in rows:
            offset = row.get("offset", row.get("at", 0.0))
            action = row.get("action", row.get("do"))
            events.append(
                TimelineEvent(
                    offset=float(offset),
                    action=action,
                    target=row.get("target"),
                    params=dict(row.get("params") or {}),
                )
            )
        return cls(events)

    @classmethod
    def from_tuples(cls, rows: Iterable[tuple]) -> ScenarioTimeline:
        """Build from ``[(offset, action[, target[, params]])]`` tuples."""
        events = []
        for row in rows:
            if len(row) == 2:
                offset, action = row
                target, params = None, {}
            elif len(row) == 3:
                offset, action, target = row
                params = {}
            elif len(row) == 4:
                offset, action, target, params = row
            else:
                raise ValueError(f"bad timeline tuple: {row!r}")
            events.append(
                TimelineEvent(float(offset), action, target, dict(params or {}))
            )
        return cls(events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):  # -> Iterator[TimelineEvent]
        return iter(self._events)

    @property
    def events(self) -> list[TimelineEvent]:
        return list(self._events)

    @property
    def duration(self) -> float:
        """Offset of the last event (0.0 for an empty timeline)."""
        return self._events[-1].offset if self._events else 0.0


NemesisHandler = Callable[[TimelineEvent], None]


class ChaosNemesis:
    """Executes a :class:`ScenarioTimeline` against a live test run.

    Parameters
    ----------
    timeline:
        The scenario to execute.
    supervisor:
        Object exposing ``kill_node(node_id)`` / ``restart_node(node_id)``
        (duck-typed; typically ``SupervisorManager``).
    broker:
        Object exposing ``matrix`` (``partition`` / ``heal_partition`` /
        ``heal_all``) and the ``set_*`` fault knobs (duck-typed; typically
        ``FileMessageBroker``).
    seed:
        RNG seed for ``corrupt_file/bitflip`` byte selection.
    """

    def __init__(
        self,
        timeline: ScenarioTimeline,
        supervisor: Any = None,
        broker: Any = None,
        seed: int = 0,
        handlers: dict[str, NemesisHandler] | None = None,
    ) -> None:
        if not isinstance(timeline, ScenarioTimeline):
            raise TypeError("timeline must be a ScenarioTimeline")
        self.timeline = timeline
        self.supervisor = supervisor
        self.broker = broker
        self._rng = random.Random(seed)
        self._handlers: dict[str, NemesisHandler] = dict(handlers or {})
        self._log: list[EventRecord] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_wall: float | None = None

    # -- custom actions ---------------------------------------------------
    def register_handler(self, action: str, handler: NemesisHandler) -> None:
        """Register (or override) the handler for *action*."""
        self._handlers[action] = handler

    # -- execution ----------------------------------------------------------
    @property
    def event_log(self) -> list[EventRecord]:
        with self._lock:
            return list(self._log)

    @property
    def started(self) -> bool:
        return self._started_wall is not None

    def run(self) -> list[EventRecord]:
        """Execute the whole timeline on this thread; return the event log.

        Event *i* fires at ``start + offset_i`` on the monotonic clock, so an
        overrunning action delays (never skips) later events; the slip is
        recorded as ``EventRecord.lateness``. Returns early if :meth:`stop`
        is called from another thread.
        """
        start_mono = time.monotonic()
        start_wall = time.time()
        with self._lock:
            self._started_wall = start_wall
        for event in self.timeline:
            planned = start_mono + event.offset
            while True:
                if self._stop_event.is_set():
                    return self.event_log
                remaining = planned - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.01))
            executed_at = time.time()
            try:
                self._dispatch(event)
                ok, error = True, ""
            except Exception as exc:  # record, then carry on with the timeline
                ok, error = False, f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._log.append(
                    EventRecord(
                        offset=event.offset,
                        action=event.action,
                        target=event.target,
                        params=dict(event.params),
                        executed_at=executed_at,
                        lateness=executed_at - (start_wall + event.offset),
                        ok=ok,
                        error=error,
                    )
                )
        return self.event_log

    def start_background(self) -> threading.Thread:
        """Run the timeline on a daemon thread; use :meth:`wait` to join."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("nemesis is already running")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run, daemon=True, name="chaos-nemesis"
        )
        self._thread.start()
        return self._thread

    def wait(self, timeout: float | None = None) -> list[EventRecord]:
        """Join the background thread; return the event log so far."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return self.event_log

    def stop(self) -> None:
        """Ask a background run to finish early (current action completes)."""
        self._stop_event.set()

    def __enter__(self) -> ChaosNemesis:
        self.start_background()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
        self.wait()

    # -- dispatch -----------------------------------------------------------
    def _dispatch(self, event: TimelineEvent) -> None:
        if event.action in self._handlers:
            self._handlers[event.action](event)
            return
        method = getattr(self, f"_do_{event.action}", None)
        if method is None:
            raise ValueError(f"no handler for action {event.action!r}")
        method(event)

    # -- built-in actions -----------------------------------------------------
    def _do_partition(self, event: TimelineEvent) -> None:
        broker = self._require_broker("partition")
        try:
            nodes_b = event.params["nodes_b"]
        except KeyError:
            raise ValueError("partition needs params={'nodes_b': [...]") from None
        broker.matrix.partition(event.target, nodes_b)

    def _do_heal(self, event: TimelineEvent) -> None:
        broker = self._require_broker("heal")
        nodes_b = event.params.get("nodes_b")
        if event.target is None and nodes_b is None:
            broker.matrix.heal_all()
        else:
            broker.matrix.heal_partition(event.target, nodes_b)

    def _do_kill(self, event: TimelineEvent) -> None:
        supervisor = self._require_supervisor("kill")
        supervisor.kill_node(event.target)

    def _do_restart(self, event: TimelineEvent) -> None:
        supervisor = self._require_supervisor("restart")
        graceful = bool(event.params.get("graceful", False))
        try:
            supervisor.restart_node(event.target, graceful=graceful)
        except TypeError:  # minimal duck-typed doubles
            supervisor.restart_node(event.target)

    def _do_corrupt_file(self, event: TimelineEvent) -> None:
        mode = event.params.get("mode", "truncate")
        if mode not in CORRUPT_MODES:
            raise ValueError(f"bad corrupt_file mode {mode!r}")
        getattr(self, f"_corrupt_{mode}")(
            event.target, **{k: v for k, v in event.params.items() if k != "mode"}
        )

    def _do_delay(self, event: TimelineEvent) -> None:
        broker = self._require_broker("delay")
        ms = float(event.params.get("ms", 0))
        if ms < 0:
            raise ValueError("delay ms must be >= 0")
        broker.set_node_latency(event.target, ms / 1000.0)

    def _do_set_loss(self, event: TimelineEvent) -> None:
        broker = self._require_broker("set_loss")
        rate = float(event.params.get("rate", 0.0))
        if event.target is None:
            broker.set_loss_rate(rate)
        else:
            broker.set_node_loss(event.target, rate)

    def _do_set_latency(self, event: TimelineEvent) -> None:
        broker = self._require_broker("set_latency")
        base = float(event.params.get("base", 0.0))
        jitter = float(event.params.get("jitter", 0.0))
        if event.target is None:
            broker.set_latency(base, jitter)
        else:
            broker.set_node_latency(event.target, base + jitter)

    # -- helpers --------------------------------------------------------------
    def _require_broker(self, action: str) -> Any:
        if self.broker is None:
            raise RuntimeError(f"action {action!r} needs a broker, none is bound")
        return self.broker

    def _require_supervisor(self, action: str) -> Any:
        if self.supervisor is None:
            raise RuntimeError(
                f"action {action!r} needs a supervisor, none is bound"
            )
        return self.supervisor

    # -- corruption primitives --------------------------------------------------
    def _corrupt_truncate(self, path: str, size: int = 0, **_: Any) -> None:
        if path is None:
            raise ValueError("corrupt_file needs a target path")
        if size < 0:
            raise ValueError("truncate size must be >= 0")
        with open(path, "r+b") as fh:
            fh.truncate(size)

    def _corrupt_bitflip(self, path: str, count: int = 1, **_: Any) -> None:
        if path is None:
            raise ValueError("corrupt_file needs a target path")
        if count < 1:
            raise ValueError("bitflip count must be >= 1")
        with open(path, "r+b") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                raise ValueError(f"cannot bitflip empty file: {path!r}")
            for _ in range(count):
                pos = self._rng.randrange(size)
                fh.seek(pos)
                byte = fh.read(1)
                fh.seek(pos)
                fh.write(bytes([byte[0] ^ (1 << self._rng.randrange(8))]))
                fh.flush()

    def _corrupt_enospc(self, path: str, size_bytes: int = 4096, **_: Any) -> None:
        """Simulate disk-pressure around *path* with a filler sidecar file."""
        if path is None:
            raise ValueError("corrupt_file needs a target path")
        if size_bytes < 0:
            raise ValueError("enospc size_bytes must be >= 0")
        sidecar = f"{path}.enospc-fill"
        with open(sidecar, "wb") as fh:
            fh.write(b"\0" * size_bytes)


# ---------------------------------------------------------------------------
# Unit self-tests (stdlib only; safe on Windows + POSIX, no network).
# ---------------------------------------------------------------------------


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile
    from pathlib import Path

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} nemesis::{name}", flush=True)

    # --- TimelineEvent / ScenarioTimeline ---
    try:
        TimelineEvent(-1.0, "kill", "n1")
        check("offset_validated", False)
    except ValueError:
        check("offset_validated", True)
    try:
        TimelineEvent(0.0, "zap", "n1")
        check("action_validated", False)
    except ValueError:
        check("action_validated", True)

    tl = ScenarioTimeline.from_tuples(
        [
            (0.2, "heal"),
            (0.0, "partition", ["n1"], {"nodes_b": ["n3"]}),
            (0.1, "kill", "n2"),
        ]
    )
    check("sorted", [e.offset for e in tl] == [0.0, 0.1, 0.2])
    check("duration", tl.duration == 0.2)
    check("len", len(tl) == 3)
    tl2 = ScenarioTimeline.from_dicts(
        [{"at": 0.0, "do": "heal"}, {"offset": 1.0, "action": "kill", "target": "n1"}]
    )
    check("from_dicts_aliases", len(tl2) == 2 and tl2.events[1].target == "n1")

    # --- ChaosNemesis against stub doubles ---
    class StubMatrix:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def partition(self, a, b):
            self.calls.append(("partition", a, b))

        def heal_partition(self, a=None, b=None):
            self.calls.append(("heal_partition", a, b))

        def heal_all(self):
            self.calls.append(("heal_all",))

    class StubBroker:
        def __init__(self) -> None:
            self.matrix = StubMatrix()
            self.calls: list[tuple] = []

        def set_node_latency(self, node, s):
            self.calls.append(("node_latency", node, s))

        def set_loss_rate(self, rate):
            self.calls.append(("loss", rate))

        def set_node_loss(self, node, rate):
            self.calls.append(("node_loss", node, rate))

        def set_latency(self, base, jitter=0.0):
            self.calls.append(("latency", base, jitter))

    class StubSupervisor:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def kill_node(self, node):
            self.calls.append(("kill", node))

        def restart_node(self, node, graceful=False):
            self.calls.append(("restart", node, graceful))

    with tempfile.TemporaryDirectory(prefix="jepsen-nemesis-") as tmp:
        victim = Path(tmp) / "data.bin"
        victim.write_bytes(b"\x00" * 64)
        trunc = Path(tmp) / "truncate.bin"
        trunc.write_bytes(b"ABCDEFGH")

        broker, sup = StubBroker(), StubSupervisor()
        timeline = ScenarioTimeline.from_tuples(
            [
                (0.00, "partition", ["n1"], {"nodes_b": ["n3"]}),
                (0.01, "delay", "n1", {"ms": 250}),
                (0.02, "set_loss", None, {"rate": 0.5}),
                (0.03, "kill", "n2"),
                (0.04, "restart", "n2", {"graceful": True}),
                (0.05, "corrupt_file", str(victim), {"mode": "bitflip", "count": 2}),
                (0.06, "corrupt_file", str(trunc), {"mode": "truncate", "size": 3}),
                (0.07, "heal"),
            ]
        )
        nem = ChaosNemesis(timeline, supervisor=sup, broker=broker, seed=42)
        log = nem.run()
        check("run_all_events", len(log) == 8 and all(r.ok for r in log))
        check(
            "partition_dispatched",
            ("partition", ["n1"], ["n3"]) in broker.matrix.calls,
        )
        check("delay_dispatched", ("node_latency", "n1", 0.25) in broker.calls)
        check("loss_dispatched", ("loss", 0.5) in broker.calls)
        check("kill_dispatched", ("kill", "n2") in sup.calls)
        check("restart_dispatched", ("restart", "n2", True) in sup.calls)
        check("bitflip_applied", victim.read_bytes() != b"\x00" * 64)
        check("truncate_applied", trunc.read_bytes() == b"ABC")
        check("heal_dispatched", ("heal_all",) in broker.matrix.calls)
        check("log_order", [r.offset for r in log] == sorted(r.offset for r in log))

        # enospc sidecar + error capture for a missing file.
        nem2 = ChaosNemesis(
            ScenarioTimeline.from_tuples(
                [
                    (0.0, "corrupt_file", str(victim), {"mode": "enospc", "size_bytes": 16}),
                    (0.0, "corrupt_file", f"{tmp}/nope.bin", {"mode": "truncate"}),
                ]
            ),
            broker=broker,
        )
        log2 = nem2.run()
        check("enospc_sidecar", Path(str(victim) + ".enospc-fill").stat().st_size == 16)
        check("error_recorded", log2[1].ok is False and log2[1].error != "")

        # Unbound supervisor surfaces as a failed event, not a crash.
        nem3 = ChaosNemesis(ScenarioTimeline.from_tuples([(0.0, "kill", "n1")]))
        log3 = nem3.run()
        check("unbound_recorded", len(log3) == 1 and log3[0].ok is False)

        # Background + context-manager protocol.
        broker2, sup2 = StubBroker(), StubSupervisor()
        tl_bg = ScenarioTimeline.from_tuples(
            [(0.0, "kill", "n9"), (0.05, "heal")]
        )
        with ChaosNemesis(tl_bg, supervisor=sup2, broker=broker2) as nem_bg:
            nem_bg.wait(timeout=10.0)
        check("background_run", ("kill", "n9") in sup2.calls)
        check("ctx_log", len(nem_bg.event_log) == 2)

        # Custom handler override.
        seen: list[str] = []
        nem4 = ChaosNemesis(ScenarioTimeline.from_tuples([(0.0, "heal")]), broker=broker2)
        nem4.register_handler("heal", lambda ev: seen.append("custom-heal"))
        log4 = nem4.run()
        check("custom_handler", seen == ["custom-heal"] and log4[0].ok)

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"nemesis self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
