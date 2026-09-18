"""Benchmark v3 suite adapters (short / long / reviewer / critic)."""

from typing import Any

from benchmark_v3.bench_harness.suites.base import SessionPaths, SuiteAdapter
from benchmark_v3.bench_harness.suites.catalog import (
    DEFAULT_ALL_KEYS,
    FAMILY_KEYS,
    SELECTABLE_KEYS,
    family_task_map,
    get_runnable,
    resolve_suite_keys,
)
from benchmark_v3.bench_harness.suites.critic import CriticSuite
from benchmark_v3.bench_harness.suites.long_task import LongTaskSuite
from benchmark_v3.bench_harness.suites.reviewer import ReviewerSuite
from benchmark_v3.bench_harness.suites.short_task import ShortTaskSuite

#: Family registry (A-condition class). Runnable keys like ``short_b``
#: resolve through :func:`get_runnable` then this map.
SUITE_REGISTRY: dict[str, type[SuiteAdapter]] = {
    "short": ShortTaskSuite,
    "long": LongTaskSuite,
    "reviewer": ReviewerSuite,
    "critic": CriticSuite,
}
if set(SUITE_REGISTRY) != set(FAMILY_KEYS):
    raise RuntimeError(
        "SUITE_REGISTRY families %s != catalog FAMILY_KEYS %s"
        % (sorted(SUITE_REGISTRY), list(FAMILY_KEYS))
    )
_FAMILY_TASKS = family_task_map()
for _family, _cls in SUITE_REGISTRY.items():
    if tuple(_cls.TASK_IDS) != _FAMILY_TASKS[_family]:
        raise RuntimeError(
            "suite %s TASK_IDS %s != catalog %s"
            % (_family, _cls.TASK_IDS, _FAMILY_TASKS[_family])
        )


def get_suite(name: str, **kwargs: Any) -> SuiteAdapter:
    """Instantiate a runnable (``short``, ``short_b``, ``long_b``, ...)."""
    spec = get_runnable(name)
    try:
        cls = SUITE_REGISTRY[spec.family]
    except KeyError:
        raise KeyError(
            f"unknown family {spec.family!r}; expected one of {sorted(SUITE_REGISTRY)}"
        ) from None
    if spec.family == "critic":
        return cls(judge_driver=kwargs.get("judge_driver"), condition=spec.condition)
    return cls(condition=spec.condition)


__all__ = [
    "SUITE_REGISTRY",
    "CriticSuite",
    "LongTaskSuite",
    "ReviewerSuite",
    "SessionPaths",
    "ShortTaskSuite",
    "SuiteAdapter",
    "DEFAULT_ALL_KEYS",
    "SELECTABLE_KEYS",
    "get_runnable",
    "get_suite",
    "resolve_suite_keys",
]
