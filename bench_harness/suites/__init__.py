"""Benchmark v3 suite adapters (short / long / reviewer / critic)."""

from benchmark_v3.bench_harness.suites.base import SessionPaths, SuiteAdapter
from benchmark_v3.bench_harness.suites.critic import CriticSuite
from benchmark_v3.bench_harness.suites.long_task import LongTaskSuite
from benchmark_v3.bench_harness.suites.reviewer import ReviewerSuite
from benchmark_v3.bench_harness.suites.short_task import ShortTaskSuite

SUITE_REGISTRY: dict[str, type[SuiteAdapter]] = {
    "short": ShortTaskSuite,
    "long": LongTaskSuite,
    "reviewer": ReviewerSuite,
    "critic": CriticSuite,
}


def get_suite(name: str) -> SuiteAdapter:
    """Instantiate the suite registered under *name* (KeyError if unknown)."""
    try:
        return SUITE_REGISTRY[name]()
    except KeyError:
        raise KeyError(
            f"unknown suite {name!r}; expected one of {sorted(SUITE_REGISTRY)}"
        ) from None


__all__ = [
    "SUITE_REGISTRY",
    "CriticSuite",
    "LongTaskSuite",
    "ReviewerSuite",
    "SessionPaths",
    "ShortTaskSuite",
    "SuiteAdapter",
    "get_suite",
]
