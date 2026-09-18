"""Single catalog of selectable runnables (suite family × A/B condition).

CLI / TUI / ``get_suite`` read this module instead of hard-coding the four
A-only names. Adding a condition or a future family is one more
:class:`Runnable` row — task content still lives in the family suite class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Runnable:
    """One checkbox / ``--suite`` value."""

    key: str
    family: str
    condition: str
    title: str
    tasks: tuple[str, ...]
    in_default_all: bool = True


_SHORT_TASKS = ("varint_parser", "timing_wheel", "lexer_state_machine")
_LONG_TASKS = ("raft_cluster", "saga_coordinator")
_REVIEWER_TASKS = ("lock_ordering", "api_drift", "bait_guard")
_CRITIC_TASKS = ("audit_bundle",)

RUNNABLES: tuple[Runnable, ...] = (
    Runnable("short", "short", "a", "short A · 契约 only", _SHORT_TASKS, True),
    Runnable("short_b", "short", "b", "short B · 契约 + frozen plan", _SHORT_TASKS, False),
    Runnable("long", "long", "a", "long A · 契约 only", _LONG_TASKS, True),
    Runnable("long_b", "long", "b", "long B · 契约 + frozen plan", _LONG_TASKS, False),
    Runnable("reviewer", "reviewer", "a", "reviewer", _REVIEWER_TASKS, True),
    Runnable("critic", "critic", "a", "critic", _CRITIC_TASKS, True),
)

_BY_KEY: dict[str, Runnable] = {r.key: r for r in RUNNABLES}

SELECTABLE_KEYS: tuple[str, ...] = tuple(r.key for r in RUNNABLES)
DEFAULT_ALL_KEYS: tuple[str, ...] = tuple(r.key for r in RUNNABLES if r.in_default_all)
FAMILY_KEYS: tuple[str, ...] = tuple(
    r.family for r in RUNNABLES if r.condition == "a" and r.in_default_all
)
CLI_SUITE_CHOICES: tuple[str, ...] = SELECTABLE_KEYS + ("all",)
SUITE_LABELS: dict[str, str] = {r.key: r.title for r in RUNNABLES}
DEFAULT_TUI_KEYS: tuple[str, ...] = DEFAULT_ALL_KEYS[:2]


def family_task_map() -> dict[str, tuple[str, ...]]:
    """A-condition family → task ids."""
    return {r.family: r.tasks for r in RUNNABLES if r.condition == "a"}


def task_family_map() -> dict[str, str]:
    """Task id → A-condition family."""
    return {task: r.family for r in RUNNABLES if r.condition == "a" for task in r.tasks}


def canonical_a_tasks() -> tuple[str, ...]:
    """Stable A-task roster for the master board (B slots use ``task@b``)."""
    seen: list[str] = []
    for r in RUNNABLES:
        if r.condition != "a":
            continue
        for task in r.tasks:
            if task not in seen:
                seen.append(task)
    return tuple(seen)


def canonical_b_tasks() -> tuple[str, ...]:
    """Tasks that have a B-condition runnable (currently short + long)."""
    seen: list[str] = []
    for r in RUNNABLES:
        if r.condition != "b":
            continue
        for task in r.tasks:
            if task not in seen:
                seen.append(task)
    return tuple(seen)


def family_b_task_map() -> dict[str, tuple[str, ...]]:
    """B-condition family → task ids."""
    return {r.family: r.tasks for r in RUNNABLES if r.condition == "b"}


def summary_sections() -> list[tuple[str, str, str, list[str]]]:
    """Rows for the CLI/Markdown per-runnable summary table."""
    return [
        (r.key, "[%s]" % r.key, r.title, list(r.tasks))
        for r in RUNNABLES
    ]


def get_runnable(key: str) -> Runnable:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown suite {key!r}; expected one of {list(SELECTABLE_KEYS)}"
        ) from None


def resolve_suite_keys(requested: str | Iterable[str]) -> list[str]:
    """Expand ``all`` to the four A-condition families; otherwise validate keys.

    Repeated keys are de-duplicated in order so one CLI invocation can take
    ``--suite short --suite long`` without running a family twice.
    """
    if isinstance(requested, str):
        keys = [requested]
    else:
        keys = list(requested)
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        chunk = list(DEFAULT_ALL_KEYS) if key == "all" else None
        if chunk is None:
            get_runnable(key)
            chunk = [key]
        for item in chunk:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


def self_test() -> tuple[int, int]:
    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} catalog::{name}", flush=True)

    check("selectable_has_b", "short_b" in SELECTABLE_KEYS and "long_b" in SELECTABLE_KEYS)
    check("all_is_a_only", resolve_suite_keys("all") == ["short", "long", "reviewer", "critic"])
    check("multi_suite_order", resolve_suite_keys(["short", "long"]) == ["short", "long"])
    check("multi_suite_dedupe", resolve_suite_keys(["short", "short", "long"]) == ["short", "long"])
    check("short_b_family", get_runnable("short_b").family == "short")
    check("short_b_condition", get_runnable("short_b").condition == "b")
    check("short_b_same_tasks", get_runnable("short_b").tasks == get_runnable("short").tasks)
    check("cli_choices_end_with_all", CLI_SUITE_CHOICES[-1] == "all")
    check("cli_choices_cover_selectable", set(SELECTABLE_KEYS) <= set(CLI_SUITE_CHOICES))
    check("family_keys_from_a", FAMILY_KEYS == ("short", "long", "reviewer", "critic"))
    check("canonical_a_count", len(canonical_a_tasks()) == 9)
    check("canonical_b_count", len(canonical_b_tasks()) == 5)
    check("canonical_b_short_long", set(canonical_b_tasks()) == set(_SHORT_TASKS + _LONG_TASKS))
    check("summary_follows_runnables", [row[0] for row in summary_sections()] == list(SELECTABLE_KEYS))
    try:
        get_runnable("nope")
        check("unknown_rejected", False)
    except KeyError:
        check("unknown_rejected", True)
    return counts[0], counts[1]
