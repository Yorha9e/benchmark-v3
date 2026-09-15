"""Jepsen-style distributed chaos engine (benchmark_v3 harness).

Subpackage layout (see ``benchmark_v3/docs/BENCHMARK_HARNESS_SPEC_V3.md``)::

    jepsen/
        __init__.py      this file (public re-exports)
        supervisor.py    multi-node process lifecycle + external SIGKILL
        broker.py        file mailbox bus + partition / fault injection
        nemesis.py       declarative chaos-timeline runner
        checker.py       distributed invariant analysis

Standard library only, cross-platform (Windows + POSIX), Python 3.11+.
"""

from . import broker as broker
from . import checker as checker
from . import nemesis as nemesis
from . import supervisor as supervisor
from .broker import FileMessageBroker, PartitionMatrix
from .checker import CheckResult, InvariantChecker
from .nemesis import ChaosNemesis, ScenarioTimeline, TimelineEvent
from .supervisor import (
    MarkerWatcher,
    NodeProcess,
    NodeState,
    NodeStatus,
    SupervisorManager,
    hard_kill_pid,
)

__version__ = "3.0.0"

__all__ = [
    "ChaosNemesis",
    "CheckResult",
    "FileMessageBroker",
    "InvariantChecker",
    "MarkerWatcher",
    "NodeProcess",
    "NodeState",
    "NodeStatus",
    "PartitionMatrix",
    "ScenarioTimeline",
    "SupervisorManager",
    "TimelineEvent",
    "broker",
    "checker",
    "hard_kill_pid",
    "nemesis",
    "supervisor",
]
