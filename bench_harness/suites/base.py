"""SuiteAdapter: unified lifecycle base class for all benchmark_v3 suites.

Lifecycle (SPEC v3, Section 2)::

    run_session(task_id, model_id, driver, output_dir, resume=False)
      1. prepare()  - WorkspaceManager sandbox + Git baseline + task fixtures
      2. execute()  - agent tool-use loop with single-prompt snapshoting
      3. evaluate() - suite-specific assertions / chaos checks  (implemented
                      by subclasses via :meth:`evaluate_task`)
      4. finalize() - EvaluationReport + atomic persistence + trace annotate

Cross-cutting guarantees implemented here (never in subclasses):

* Single-prompt snapshot & resume replay via
  :class:`~benchmark_v3.bench_harness.core.snapshot.SnapshotManager`:
  the exact request payload of every driver call is atomically persisted
  *before* the call; ``resume=True`` re-issues only that one stalled turn.
* 10M token safety fuse (:data:`TOKEN_BUDGET`): the loop stops as soon as
  the cumulative total reaches the budget; the overrun is recorded in
  :class:`TokenAuditMetrics` (auditing only, never deducted from score).
* Time is 100% pure telemetry (:class:`TelemetryMetrics`): wall-clock and
  retry counts are recorded for Pareto / triage use and never influence
  the functional score.
* Streaming trace capture (:class:`TraceCollector` -> ``wire.jsonl`` +
  ``trajectory.json``) and live progress
  (:class:`ProgressReporter` + ``live_status.json`` sidecar).

Only the Python standard library plus the M1/M2/M3 harness modules are
used. A deterministic :class:`ScriptedDriver` is provided for unit
self-tests and offline smoke runs.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.core.report import ReportManager
from benchmark_v3.bench_harness.core.reporter import ProgressReporter
from benchmark_v3.bench_harness.core.runner import ProcessRunner
from benchmark_v3.bench_harness.core.snapshot import SnapshotManager
from benchmark_v3.bench_harness.core.types import (
    TOKEN_BUDGET,
    AgentTrajectory,
    EvaluationReport,
    MilestoneResult,
    TelemetryMetrics,
    TokenAuditMetrics,
)
from benchmark_v3.bench_harness.core.workspace import WorkspaceManager
from benchmark_v3.bench_harness.drivers.base import BaseDriver, DriverResponse
from benchmark_v3.bench_harness.trace.annotator import TraceAnnotator
from benchmark_v3.bench_harness.trace.collector import TraceCollector

__all__ = [
    "AGENT_TOOLS",
    "FINISH_TOOL_NAME",
    "MIN_FINISH_SUMMARY_CHARS",
    "ScriptedDriver",
    "SessionPaths",
    "SuiteAdapter",
    "mk_milestone",
    "scripted_finish",
    "utc_now_iso",
]


#: Notices appended to every suite's initial prompt.
BENCHMARK_NOTICE = (
    "\n\nContext: you are a candidate in an offline software-engineering "
    "benchmark. Every file in this workspace is a synthetic fixture written "
    "for scoring. There is no production system, no live network, no real "
    "user data, and no request to attack anything. Completing the stated "
    "deliverable is expected and authorized."
)

WORKSPACE_BOUNDARY_NOTICE = (
    "\n\nWorkspace boundary (hard rule): you may ONLY read, write, and run "
    "commands inside the current working directory. Paths containing `..`, "
    "absolute paths (e.g. `/etc/...`, `C:\\...`), home-directory references "
    "(`~`, `$HOME`), and `file:` URIs are refused with an ERROR. The task is "
    "fully self-contained — there is nothing useful outside the workspace, "
    "do not look for it."
)

FINISH_TOOL_NAME = "finish"
MIN_FINISH_SUMMARY_CHARS = 20

DELIVERABLE_NOTICE = (
    "\n\nDeliverable (hard rule): scoring only reads files on disk in this "
    "workspace. Prototyping in `bash` (for example `python -c ...`) does "
    "NOT count. You MUST persist the required artifact with the `write` or "
    "`edit` tool — typically `solution.py`, `raft.py`/`saga.py`, "
    "`audit.json`, or the patched target named in `TASK.md` — before you "
    "call `finish`. Calling `finish` without that file on disk scores as "
    "incomplete."
)

TASK_FINISH_NOTICE = (
    "\n\nTask completion (hard rule): the only way to end this task is to "
    "call the `finish` tool with a non-empty `summary` of what you "
    "implemented, which files you changed, and how you checked it. Call "
    "`finish` only after the deliverable file exists on disk. A reply with "
    "no tool calls does NOT end the task — keep working or call `finish`."
)

MISSING_FINISH_NUDGE = (
    "[system] The task is still running. The only way to end it is to call "
    "the `finish` tool with a non-empty `summary` (what you implemented, "
    "which files you changed, and how you checked). A message with no tool "
    "calls does not end the task."
)

EMPTY_ABORT_NUDGE = (
    "[system] The previous model turn was empty (no text, no tool calls, no "
    "tokens). That is an aborted turn, not completion. Continue implementing, "
    "or call `finish` with a summary of at least "
    f"{MIN_FINISH_SUMMARY_CHARS} characters (files changed, what you "
    "implemented, how you checked). A silent reply cannot end the task."
)

BENCHMARK_REFUSAL_NUDGE = (
    "[system] This workspace is an offline software-engineering benchmark "
    "with synthetic fixtures only. A policy refusal does not end the task "
    "and scores as incomplete. Write the required deliverable (review JSON, "
    "implementation, or patch) — do not produce attack payloads — or call "
    "`finish` only after that file exists."
)

_REFUSAL_HINTS = (
    "can't help",
    "cannot help",
    "cannot fulfill",
    "can't assist",
    "cannot assist",
    "unable to help",
    "unable to assist",
    "i refuse",
    "against my policy",
    "sorry, i cannot",
    "sorry, i can't",
)


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


#: Shell metacharacter-adjacent ``..`` segments: ``../``, ``..\``, bare
#: ``..`` as a path component (``cd ..``, ``cat ../x``). Matched
#: case-insensitively against the raw command string.
_PARENT_TRAVERSAL_RE = None  # lazy-compiled (see _command_escapes_workspace)


def _bulk_kill_reason(command: str) -> str | None:
    """Screen process-kill commands that would tear down the harness itself.

    The task runs inside the harness's **own** interpreter, so an image-name
    kill (``taskkill /F /IM python.exe``), a bare ``taskkill`` with no
    ``/PID``, or a name-wide ``pkill``/``killall`` kills the evaluator
    mid-run: no traceback, no finalize, and the run is stranded at
    ``status: running`` forever. Observed in the wild on 2026-09-20 — a model
    ran ``taskkill /F /IM python.exe`` to clean up its own test nodes and
    silently killed the benchmark.

    Only **literal-PID** kills stay allowed (``taskkill /F /PID 1234``): a
    model cleaning up its own node processes is legitimate, and the harness
    wipes leftover runtime state itself. A PID given as a shell variable
    (``//PID $p`` after enumerating every python process) is refused too,
    since that is the same bulk kill in disguise.

    A kill verb only counts when it carries a kill flag (``/F``, ``/IM``,
    ``/PID``, ``-Force``), so prose and searches that merely mention the word
    (``grep -r taskkill .``) stay allowed. Like
    :func:`_command_escapes_workspace`, this is a best-effort static screen:
    string-obfuscated invocations are out of scope.
    """
    import re

    text = command or ""
    # Strip quoted string literals and heredoc bodies so that mentioning a
    # kill verb in text (searching, echoing, or writing source code) is not
    # mistaken for invoking it.
    screened = re.sub(r"\"(?:[^\"\\\\]|\\\\.)*\"", " ", text)
    screened = re.sub(r"'(?:[^'\\\\]|\\\\.)*'", " ", screened)
    # `kill -9 -1` broadcasts to every process the user owns.
    if re.search(r"(^|[\s;|&`$()'\"=])kill\s+(?:-\S+\s+)*-?1\b", screened):
        return ("broadcast signal (`kill -1`) is not allowed; it would also "
                "terminate the harness running this task")
    # Name-wide killers: pkill/killall are name-based by construction.
    if re.search(r"(^|[\s;|&`$()'\"=])(?:pkill|killall)\b", screened, re.IGNORECASE):
        return ("name-wide process kill (`pkill`/`killall`) is not allowed; "
                "terminating processes by name also kills the harness running "
                "this task — the harness cleans up its own processes")
    # `kill` on an expanded/globbed target (e.g. every python PID enumerated
    # first). `$!` is the caller's own background job and stays allowed.
    m = re.search(r"(^|[\s;|&`$()'\"=])kill\s+((?:-\S+\s+)*)([^;|&\n]+)", screened)
    if m:
        flags, targets = m.group(2), m.group(3)
        if not re.search(r"-l\b", flags) and re.search(r"[\$`*]", targets) \
                and not re.fullmatch(r"\s*\$\!?\s*", targets):
            return ("`kill` on an expanded target is not allowed; it may match "
                    "the harness process — kill by a literal PID instead")
    # Host-shutdown or system disruption commands
    if re.search(r"(^|[\s;|&`$()'\"=])(?:shutdown|stop-computer)\b", text, re.IGNORECASE):
        return ("system shutdown commands (`shutdown`, `Stop-Computer`) are not allowed; "
                "they would terminate the benchmark harness and machine")
    # WMI process termination: `wmic process ... call terminate` / `delete`
    if re.search(r"\bwmic\b", text, re.IGNORECASE) and re.search(r"\b(?:terminate|delete)\b", text, re.IGNORECASE):
        return ("WMI process termination (`wmic ... terminate/delete`) is not allowed; "
                "it can kill the harness process — kill by explicit PID instead")
    # Legacy Windows tskill
    if re.search(r"(^|[\s;|&`$()'\"=])tskill\b", text, re.IGNORECASE):
        return ("`tskill` is not allowed; kill by explicit PID with `taskkill /PID <n>` instead")
    # Prohibit inspecting parent process to prevent two-step PID-based harness kills
    if re.search(r"\bparentprocessid\b", text, re.IGNORECASE):
        return ("inspecting parent process ID is not allowed")

    # taskkill / Stop-Process: require a kill flag, then demand literal PIDs.
    # Match the RAW text here (not the quote-stripped copy): nested quotes make
    # stripping swallow the whole invocation, and a false positive merely tells
    # the model to use a literal PID — whereas a miss kills the benchmark.
    if not re.search(r"(?:taskkill|stop-process)\b", text, re.IGNORECASE):
        return None
    if not re.search(r"(?:/f\b|/im\b|/pid\b|-force\b|-id\b)", text, re.IGNORECASE):
        return None  # mere mention (grep/echo/cat) — not an invocation
    # taskkill /IM <image>  or  Stop-Process -Name <name>
    if re.search(r"/im\s+\S", text, re.IGNORECASE) or \
       re.search(r"-name\s+\S", text, re.IGNORECASE):
        return ("process kill by image name (`/IM`, `-Name`) is not allowed; "
                "the harness itself runs as python.exe, so this would kill the "
                "benchmark mid-run — kill by explicit PID instead")
    # Every targeted PID must be a literal number.
    pids = re.findall(r"(?:/pid|-id)\s+[\"']?([^\s\"';|&)]+)", text, re.IGNORECASE)
    if not pids:
        return ("`taskkill` without an explicit `/PID` is not allowed; the "
                "harness cleans up its own processes — kill by explicit PID "
                "only if you must")
    for pid in pids:
        if not pid.isdigit():
            return ("process kill with a non-literal PID (%r) is not allowed; "
                    "enumerating every python process and killing it also kills "
                    "the harness — use a literal PID" % pid)
    return None


def _command_escapes_workspace(command: str) -> str | None:
    """Best-effort static screen: does this shell command reach outside cwd?

    Each ``bash`` tool call runs as a fresh, short-lived shell rooted at the
    task workspace, so per-command screening is sufficient (no persistent
    ``cd`` state survives between calls). Returns a human-readable reason
    when the command must be refused, else ``None``.

    Blocked: parent traversal (``..``), absolute POSIX paths, Windows
    drive/UNC paths, home-directory expansion (``~``, ``$HOME``,
    ``$env:...``), opaque PowerShell ``-EncodedCommand`` blobs (which defeat
    static screening — plaintext ``-Command`` stays allowed), git
    history/object readers (backstop behind the ``GIT_CEILING_DIRECTORIES``
    env isolation), and well-known env-var indirection out of the workspace.
    This is a defense-in-depth screen, not a shell sandbox: exotic bypasses
    (``$TMPDIR`` tricks, ``/proc`` self-fd games) are out of scope.
    """
    import re

    global _PARENT_TRAVERSAL_RE
    if _PARENT_TRAVERSAL_RE is None:
        _PARENT_TRAVERSAL_RE = re.compile(
            r"(^|[\s;|&`$()'\"=<>])\.\.(?=$|[\s;|&`$()'\"=<>/:\\])"
            r"|[/\\]\.\.(?=$|[/\\])"  # embedded segments: codebase/../../x
        )
    text = command or ""
    if _PARENT_TRAVERSAL_RE.search(text):
        return "parent-directory traversal (`..`) is not allowed; stay inside the workspace"
    kill_reason = _bulk_kill_reason(text)
    if kill_reason is not None:
        return kill_reason
    lowered = text.lower()
    # 环境变量引用（支持 $HOME, ${HOME}, %HOME%, $env:HOME 等，避免误伤普通的包含大写单词的文本或文件名）
    if re.search(r"(?:\$|\%|\$env:|\$\{)(?:HOME|USERPROFILE|HOMEDRIVE|HOMEPATH|APPDATA|LOCALAPPDATA|PROGRAMDATA)\b", text, re.IGNORECASE):
        return "home-directory reference (HOME/USERPROFILE/…) is not allowed; stay inside the workspace"
    # Git escape hatches: re-pointing git at the harness repo would bypass the
    # GIT_CEILING_DIRECTORIES isolation injected at execution time.
    if re.search(r"\bgit_ceiling_directories\b|\bgit_dir\b|--git-dir|--work-tree", lowered):
        return "git repository overrides are not allowed inside the workspace"
    # Backstop behind the ceiling: history/object readers have no legitimate
    # use inside task workspaces (fixtures are plain files, not a checkout).
    if re.search(r"(^|[\s;|&`$()'\"=])git\s+"
                 r"(?:-C\s*\S+\s+|-c\s*\S+\s+)*"
                 r"(log|show|diff|grep|blame|reflog|cat-file|ls-tree|ls-files|"
                 r"rev-list|rev-parse|stash|checkout|clean|reset)\b", lowered):
        return "git history/object access is not available inside the task workspace"
    # Opaque encoded blobs cannot be screened: force plaintext -Command.
    if re.search(r"(^|[\s;|&`$()'\"=])-EncodedCommand\b", text, re.IGNORECASE):
        return "opaque -EncodedCommand blobs are not allowed; use plaintext -Command instead"
    # `/dev/null`  drains are harmless and idiomatic; exempt before screening.
    screened = re.sub(r"/dev/null\b", "", text)
    # URL schemes are network-ish or pseudo-protocols; disallow file: explicitly below, but allow http(s)://
    screened = re.sub(r"https?://\S*", "", screened)
    # Absolute POSIX path (starts with / followed by directory name, or root `/`), or home expansion (~ or ~/...)
    if re.search(r"(?:^|[\s;|&`$()'\"=><])(?:~[/\w.~]*|/(?:[a-zA-Z0-9_.-]+(?:/|$)|$))", screened):
        # 排除普通的算术除法表达式，如 '1 / 2' 或 ' / ' 前后均为空格的纯符号
        if not re.search(r"^\s*$", screened) and not re.search(r"\s+/\s+\d+", screened):
            return "absolute path / home expansion is not allowed; use workspace-relative paths"
    # Windows drive-letter (C:\, C:/), UNC (\\host) or drive-relative (\dir) paths.
    if re.search(r"(^|[\s;|&`$()'\"=><])([a-z]:[\\/]|\\\\[\w.]|\\[\w.][\w.]*[\\/])", screened, re.IGNORECASE):
        return "absolute Windows path is not allowed; use workspace-relative paths"
    # file: URIs are absolute filesystem references regardless of slashes.
    if re.search(r"(^|[\s;|&`$()'\"=><])file:(?:[\\/]+|[a-zA-Z]:)", screened, re.IGNORECASE):
        return "file: URI access outside the workspace is not allowed"
    return None


def mk_milestone(
    milestone_id: str,
    name: str,
    passed: bool,
    score: float | None = None,
    failure_reason: str | None = None,
    diagnostics: str = "",
) -> MilestoneResult:
    """Build a :class:`MilestoneResult` (score defaults to 1.0 / 0.0)."""
    return MilestoneResult(
        milestone_id=milestone_id,
        name=name,
        passed=bool(passed),
        score=1.0 if score is None and passed else (0.0 if score is None else float(score)),
        failure_reason=None if passed else failure_reason,
        diagnostics=diagnostics or "",
    )


#: Tool declarations offered to the model (OpenAI function shape).
AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write (create or overwrite) a workspace-relative file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a workspace-relative file (truncated past 20000 chars).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace the first occurrence of `old` with `new` in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command inside the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": FINISH_TOOL_NAME,
            "description": (
                "End this task so the harness can score the workspace. Call "
                "only when the deliverable is written. Requires a short "
                "summary of what you implemented, which files you changed, "
                "and how you checked the work. A reply with no tool calls "
                "does not end the task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "Non-empty wrap-up: files changed, what the "
                            "implementation does, and how you verified it. "
                            f"At least {MIN_FINISH_SUMMARY_CHARS} characters."
                        ),
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


def normalize_finish_summary(raw: Any) -> str | None:
    """Return a stripped summary, or ``None`` when it is too short to finish."""
    text = str(raw or "").strip()
    if len(text) < MIN_FINISH_SUMMARY_CHARS:
        return None
    return text


def scripted_finish(
    summary: str = "Deliverable written; files updated; ready to be scored.",
    call_id: str = "fin_1",
) -> dict[str, Any]:
    """Canned ScriptedDriver turn that ends a session via ``finish``."""
    return {
        "content": "calling finish",
        "tool_calls": [
            {
                "id": call_id,
                "name": FINISH_TOOL_NAME,
                "arguments": {"summary": summary},
            }
        ],
    }


class ScriptedDriver(BaseDriver):
    """Deterministic offline driver replaying a canned response script.

    Each script entry is a ``dict`` with optional ``content``, ``thought``,
    ``tool_calls`` (``[{id?, name, arguments}]``) and ``token_usage``.
    When the script is exhausted, empty responses are returned; the agent
    loop treats those as incomplete turns (not task completion) until
    ``finish`` is called or the token fuse trips.
    """

    def __init__(
        self,
        model_id: str = "scripted",
        script: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, **kwargs)
        self._script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> DriverResponse:
        self.calls.append({"n_messages": len(messages)})
        if self._script:
            entry = self._script.pop(0)
        else:
            entry = {}
        tool_calls = self.normalize_tool_calls(entry.get("tool_calls"))
        usage = self.extract_token_usage(
            entry.get("token_usage", {"prompt_tokens": 10, "completion_tokens": 5})
        )
        self.account_usage(usage)
        return DriverResponse(
            content=str(entry.get("content", "")),
            thought=str(entry.get("thought", "")),
            tool_calls=tool_calls,
            token_usage=usage,
        )


@dataclass
class SessionPaths:
    """On-disk layout for one ``(suite, task)`` session."""

    root: Path
    workspace_dir: Path
    evaluation_path: Path
    summary_path: Path
    trajectory_path: Path
    wire_path: Path

    @classmethod
    def build(cls, output_dir: str | Path, suite_name: str, task_id: str) -> SessionPaths:
        root = Path(output_dir) / suite_name / task_id
        workspace_dir = root / "workspace"
        root.mkdir(parents=True, exist_ok=True)
        return cls(
            root=root,
            workspace_dir=workspace_dir,
            evaluation_path=root / "evaluation.json",
            summary_path=root / "summary.json",
            trajectory_path=root / "trajectory.json",
            wire_path=root / "wire.jsonl",
        )


@dataclass
class _LoopState:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    budget_exceeded: bool = False
    turns_used: int = 0
    tools_executed: int = 0
    task_finished: bool = False
    finish_summary: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class SuiteAdapter(ABC):
    """Abstract base for the four benchmark suites.

    Subclasses provide task content by implementing :meth:`prepare_task`,
    :meth:`build_prompt` and :meth:`evaluate_task`; this base class owns
    the whole session lifecycle, token fuse, telemetry, snapshots,
    tracing and persistence.
    """

    #: Family key (short/long/reviewer/critic). Output dir uses :attr:`run_key`.
    suite_name: str = "base"
    #: Ordered task ids of this suite.
    TASK_IDS: tuple[str, ...] = ()
    #: Optional hard turn cap. ``None`` = no turn limit (token fuse still
    #: applies; empty upstream aborts are nudged and retried).
    max_turns: int | None = None

    def __init__(self, *, condition: str = "a") -> None:
        self.condition = "b" if condition == "b" else "a"

    @property
    def run_key(self) -> str:
        """On-disk / CLI key: ``short`` or ``short_b``."""
        return f"{self.suite_name}_b" if self.condition == "b" else self.suite_name

    # -- task content (subclass API) --------------------------------------

    @classmethod
    def task_ids(cls) -> list[str]:
        return list(cls.TASK_IDS)

    @abstractmethod
    def describe_task(self, task_id: str) -> dict[str, Any]:
        """Return ``{title, brief, ...}`` metadata for *task_id*."""

    @abstractmethod
    def prepare_task(self, task_id: str, workspace_dir: Path) -> None:
        """Write task fixtures (brief, templates, buggy targets) to workspace."""

    @abstractmethod
    def build_prompt(self, task_id: str, workspace_dir: Path) -> str:
        """Build the initial user prompt for the agent loop."""

    @abstractmethod
    def evaluate_task(
        self,
        task_id: str,
        workspace_dir: Path,
        trajectory: AgentTrajectory,
    ) -> tuple[list[MilestoneResult], dict[str, Any]]:
        """Score the workspace state.

        Returns ``(milestones, extras)`` where ``extras`` may carry
        ``ast_diff_penalty``, ``peak_memory_bytes`` and ``safety_refusal``.
        """

    # -- scoring hook ------------------------------------------------------

    _B_PLAN_APPENDIX = (
        "\n\nThe specification is `TASK.md` in this workspace. Use the `read` "
        "tool on `TASK.md` before you edit. `PLAN.md` is only a suggested "
        "order of work, not a spec. If they differ, follow `TASK.md`.\n"
    )

    def _maybe_write_b_plan(self, task_id: str, workspace_dir: Path) -> None:
        if self.condition != "b":
            return
        from benchmark_v3.bench_harness.suites.b_plans import plan_for

        (workspace_dir / "PLAN.md").write_text(plan_for(task_id), encoding="utf-8")

    def _b_plan_prompt_appendix(self) -> str:
        return self._B_PLAN_APPENDIX if self.condition == "b" else ""

    def compute_final_reward(
        self,
        milestones: list[MilestoneResult],
        extras: dict[str, Any],  # noqa: ARG002 - hook signature
    ) -> float:
        """Mean milestone score (0.0 when there are no milestones)."""
        if not milestones:
            return 0.0
        return sum(m.score for m in milestones) / len(milestones)

    # -- lifecycle: prepare -------------------------------------------------

    def prepare(
        self,
        task_id: str,
        paths: SessionPaths,
        reporter: ProgressReporter,
        resume: bool = False,
    ) -> WorkspaceManager:
        reporter.update(task_id, "running", "prepare: workspace setup")
        workspace = WorkspaceManager(paths.root, "workspace", keep_on_cleanup=True)
        workspace.setup()
        snapshot = SnapshotManager(paths.root)
        if not resume:
            snapshot.clear()
        elif snapshot.has_snapshot():
            reporter.update(task_id, "running", "prepare: resume snapshot found")
        self.prepare_task(task_id, workspace.workspace_dir)
        self._maybe_write_b_plan(task_id, workspace.workspace_dir)
        workspace.ensure_git_baseline()
        return workspace

    # -- lifecycle: execute ---------------------------------------------------

    def execute(
        self,
        task_id: str,
        model_id: str,
        driver: BaseDriver,
        workspace: WorkspaceManager,
        paths: SessionPaths,
        reporter: ProgressReporter,
        resume: bool = False,
    ) -> tuple[AgentTrajectory, _LoopState]:
        session_id = f"{self.run_key}-{task_id}-{uuid.uuid4().hex[:8]}"
        collector = TraceCollector(wire_path=paths.wire_path)
        collector.start_session(session_id, task_id, model_id)
        snapshot = SnapshotManager(paths.root)
        runner = ProcessRunner(default_timeout=60.0)

        prompt = self.build_prompt(task_id, workspace.workspace_dir)
        prompt = (
            prompt
            + self._b_plan_prompt_appendix()
            + BENCHMARK_NOTICE
            + WORKSPACE_BOUNDARY_NOTICE
            + DELIVERABLE_NOTICE
            + TASK_FINISH_NOTICE
        )
        state = _LoopState()
        wall_start = time.monotonic()
        turn = 0
        is_resumed = False
        if resume:
            saved = snapshot.load()
            if saved and isinstance(saved.get("request"), dict):
                request = saved["request"]
                if isinstance(request.get("messages"), list) and request["messages"]:
                    messages = request["messages"]
                    is_resumed = True
                try:
                    turn = int(saved.get("turn_index", 0))
                except (TypeError, ValueError):
                    turn = 0
                collector.load_existing_messages(messages)
                reporter.update(task_id, "retry", f"resuming at turn {turn} with full context ({len(messages)} msgs)")

        if not is_resumed:
            messages = [{"role": "user", "content": prompt}]
            collector.record_user_turn(prompt)

        from benchmark_v3.bench_harness.core.run_manifest import (
            TaskAbandoned,
            pause_requested,
            run_dir_from_task_paths,
        )
        run_root = run_dir_from_task_paths(paths.root)

        while True:
            if pause_requested(run_root):
                raise TaskAbandoned("PAUSE.request")
            if self.max_turns is not None and turn >= self.max_turns:
                state.errors.append(f"max_turns ({self.max_turns}) reached without finish")
                reporter.update(task_id, "retry", f"max_turns={self.max_turns} reached")
                break
            # Atomic single-prompt snapshot BEFORE the driver call.
            snapshot.save(
                turn,
                {"messages": messages, "tools": AGENT_TOOLS},
                extra={
                    "task_id": task_id,
                    "model_id": model_id,
                    "session_id": session_id,
                    "timestamp": utc_now_iso(),
                },
            )
            try:
                response = driver.chat(messages, AGENT_TOOLS)
            except (KeyboardInterrupt, TaskAbandoned):
                raise
            except Exception as exc:  # driver-level fatal: seal trajectory
                err_msg = f"driver_error: {type(exc).__name__}: {exc}"
                state.errors.append(err_msg)
                reporter.update(task_id, "failed", err_msg, turn=turn)
                sys.stderr.write(f"\033[31;1m[task-error] {task_id} turn {turn}: {err_msg}\033[0m\n")
                sys.stderr.flush()
                break
            usage = BaseDriver.extract_token_usage(response.token_usage)
            state.prompt_tokens += usage.get("prompt_tokens", 0)
            state.completion_tokens += usage.get("completion_tokens", 0)
            state.reasoning_tokens += usage.get("reasoning_tokens", 0)
            state.turns_used += 1

            turn_record = collector.record_assistant_turn(
                content=response.content,
                thought=response.thought,
                tool_calls=[
                    {
                        "call_id": c.get("id", ""),
                        "tool_name": c.get("name", ""),
                        "arguments": c.get("arguments", {}),
                    }
                    for c in response.tool_calls
                ],
                tokens=usage,
            )
            _ = turn_record

            # 10M token safety fuse (auditing only — never a score deduction).
            if state.total >= TOKEN_BUDGET:
                state.budget_exceeded = True
                collector.record_user_turn(
                    "[system] token budget (10M) reached; stopping agent loop."
                )
                reporter.update(task_id, "retry", f"token fuse tripped at turn {turn}")
                break

            if not response.tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content or None,
                }
                if response.thought:
                    assistant_msg["reasoning_content"] = response.thought
                messages.append(assistant_msg)
                nudge = self._incomplete_turn_nudge(response, usage)
                messages.append({"role": "user", "content": nudge})
                collector.record_user_turn(nudge)
                reporter.update(
                    task_id,
                    "running",
                    f"turn {turn}: waiting for finish (no tool calls)",
                    turn=turn,
                    tokens=state.total,
                )
                turn += 1
                continue

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or None,
                "tool_calls": [
                    {
                        "id": c.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": c.get("name", ""),
                            "arguments": json.dumps(
                                c.get("arguments", {}), ensure_ascii=False
                            ),
                        },
                    }
                    for c in response.tool_calls
                ],
            }
            if response.thought:
                assistant_msg["reasoning_content"] = response.thought
            messages.append(assistant_msg)
            for call in response.tool_calls:
                call_id = str(call.get("id", ""))
                tool_name = str(call.get("name", ""))
                arguments = call.get("arguments", {}) or {}
                if not call_id:
                    call_id = BaseDriver.new_call_id("call")
                output = self.execute_tool(
                    tool_name, arguments, workspace, runner, state
                )
                state.tools_executed += 1
                collector.record_tool_result(
                    call_id=call_id,
                    tool_name=tool_name,
                    stdout=output,
                    exit_code=0 if not output.startswith("ERROR:") else 1,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "content": output,
                    }
                )
                if tool_name == FINISH_TOOL_NAME and state.task_finished:
                    self._persist_finish_summary(paths, state)
            reporter.update(
                task_id,
                "running",
                f"turn {turn}: {state.tools_executed} tool calls",
                turn=turn,
                tokens=state.total,
            )
            if state.task_finished:
                reporter.update(
                    task_id,
                    "running",
                    f"turn {turn}: finish accepted",
                    turn=turn,
                    tokens=state.total,
                )
                break
            turn += 1

        wall_seconds = time.monotonic() - wall_start
        trajectory = collector.finish(wall_time_seconds=wall_seconds)
        try:
            collector.save_trajectory(paths.trajectory_path)
        except OSError:
            pass
        collector.close()
        return trajectory, state

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        workspace: WorkspaceManager,
        runner: ProcessRunner,
        state: _LoopState,  # noqa: ARG002 - hook signature
    ) -> str:
        """Run one agent tool call against the workspace; returns text output."""
        try:
            if tool_name == "write":
                target = workspace.resolve(str(arguments.get("path", "")))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(arguments.get("content", "")), encoding="utf-8")
                return f"OK: wrote {len(str(arguments.get('content', '')))} chars to {target.name}"
            if tool_name == "read":
                target = workspace.resolve(str(arguments.get("path", "")))
                text = target.read_text(encoding="utf-8")
                if len(text) > 20000:
                    text = text[:20000] + f"\n...[truncated {len(text) - 20000} chars]"
                return text or "(empty file)"
            if tool_name == "edit":
                target = workspace.resolve(str(arguments.get("path", "")))
                old = str(arguments.get("old", ""))
                new = str(arguments.get("new", ""))
                text = target.read_text(encoding="utf-8")
                if old not in text:
                    return "ERROR: `old` string not found in file"
                target.write_text(text.replace(old, new, 1), encoding="utf-8")
                return "OK: edit applied"
            if tool_name == FINISH_TOOL_NAME:
                summary = normalize_finish_summary(arguments.get("summary"))
                if summary is None:
                    return (
                        "ERROR: `finish` requires a `summary` of at least "
                        f"{MIN_FINISH_SUMMARY_CHARS} characters describing "
                        "what you changed and how you checked it. The task "
                        "is still running."
                    )
                state.task_finished = True
                state.finish_summary = summary
                return f"OK: task finished. summary_chars={len(summary)}"
            if tool_name == "bash":
                command = str(arguments.get("command", ""))
                violation = _command_escapes_workspace(command)
                if violation is not None:
                    try:
                        state.errors.append(f"containment: refused bash ({violation})")
                    except Exception:
                        pass
                    return f"ERROR: {violation}"
                try:
                    timeout = float(arguments.get("timeout", 30.0) or 30.0)
                except (TypeError, ValueError):
                    timeout = 30.0
                timeout = max(1.0, min(timeout, 120.0))
                # Blind git to the harness repo above the workspace: the ceiling
                # must be the workspace PARENT (a ceiling equal to cwd does not
                # stop ascent), so `git log` / `git show <commit>:<path>` cannot
                # become a read primitive for grading keys outside the sandbox.
                # NOTE: must be ABSOLUTE — git silently ignores relative
                # GIT_CEILING_DIRECTORIES entries (the default output layout is
                # relative, so resolve here, not at the call site).
                ceiling = str(Path(os.path.abspath(workspace.workspace_dir)).parent)
                result = runner.run(
                    command,
                    shell=True,
                    cwd=workspace.workspace_dir,
                    timeout=timeout,
                    extra_env={"GIT_CEILING_DIRECTORIES": ceiling},
                )
                output = (result.stdout or "") + (
                    f"\n[stderr]\n{result.stderr}" if result.stderr else ""
                )
                if result.timed_out:
                    output += f"\n[TIMEOUT after {timeout}s]"
                if len(output) > 20000:
                    output = output[:20000] + "\n...[truncated]"
                return output.strip() or f"(exit {result.returncode}, no output)"
            return f"ERROR: unknown tool {tool_name!r}"
        except ValueError as exc:
            return f"ERROR: {exc}"
        except OSError as exc:
            return f"ERROR: filesystem: {exc}"

    @staticmethod
    def _incomplete_turn_nudge(
        response: DriverResponse, usage: dict[str, int]
    ) -> str:
        empty = not str(response.content or "").strip() and not str(
            response.thought or ""
        ).strip()
        zero = not (
            int(usage.get("prompt_tokens", 0) or 0)
            + int(usage.get("completion_tokens", 0) or 0)
            + int(usage.get("reasoning_tokens", 0) or 0)
        )
        if empty and zero:
            return EMPTY_ABORT_NUDGE
        blob = f"{response.content or ''} {response.thought or ''}".lower()
        if any(hint in blob for hint in _REFUSAL_HINTS):
            return BENCHMARK_REFUSAL_NUDGE
        return MISSING_FINISH_NUDGE

    @staticmethod
    def _persist_finish_summary(paths: SessionPaths, state: _LoopState) -> None:
        if not state.finish_summary:
            return
        try:
            (paths.root / "finish_summary.txt").write_text(
                state.finish_summary, encoding="utf-8"
            )
        except OSError:
            pass

    # -- lifecycle: evaluate + finalize ---------------------------------------

    def finalize(
        self,
        task_id: str,
        model_id: str,
        workspace: WorkspaceManager,
        trajectory: AgentTrajectory,
        state: _LoopState,
        wall_seconds: float,
        driver: BaseDriver,
        paths: SessionPaths,
        reporter: ProgressReporter,
    ) -> EvaluationReport:
        reporter.update(task_id, "running", "evaluate: running assertions")
        try:
            milestones, extras = self.evaluate_task(
                task_id, workspace.workspace_dir, trajectory
            )
        except Exception as exc:  # a crashing evaluator fails closed, never hangs CLI
            milestones = [
                mk_milestone(
                    f"{task_id}_evaluator_crash",
                    "Evaluator completed without crashing",
                    False,
                    failure_reason=f"evaluator_crash: {type(exc).__name__}: {exc}",
                )
            ]
            extras = {}
        passed_count = sum(1 for m in milestones if m.passed)
        token_metrics = TokenAuditMetrics.from_usage(
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            reasoning_tokens=state.reasoning_tokens,
            passed_milestones=passed_count,
        )
        if state.budget_exceeded:
            token_metrics = TokenAuditMetrics(
                prompt_tokens=token_metrics.prompt_tokens,
                completion_tokens=token_metrics.completion_tokens,
                reasoning_tokens=token_metrics.reasoning_tokens,
                total_tokens=token_metrics.total_tokens,
                tokens_per_passed_milestone=token_metrics.tokens_per_passed_milestone,
                budget_exceeded=True,
            )
        telemetry = TelemetryMetrics(
            wall_time_seconds=wall_seconds,  # pure telemetry — never scored
            reasoning_time_seconds=0.0,
            network_retry_count=int(getattr(driver, "retry_count", 0) or 0),
        )
        final_reward = self.compute_final_reward(milestones, extras)

        # An aborted session must NOT be filed as a legitimate 0-score result:
        # that would let an upstream outage masquerade as "the model failed the
        # task" and silently corrupt the leaderboard.
        #
        # Two abort shapes are covered (both need evidence of a transport
        # failure, so a model that simply stops early is still scored honestly):
        #   * died before completing any turn (0 turns, 0 tokens), and
        #   * hung mid-task: turns completed but no `finish`, a recorded driver
        #     error, and output far too small to have been a real attempt.
        minimal_output = token_metrics.completion_tokens < 5_000
        aborted = (
            not state.finish_summary
            and bool(state.errors)
            and (
                (state.turns_used == 0 and token_metrics.total_tokens == 0)
                or (state.turns_used > 0 and minimal_output)
            )
        )

        report = EvaluationReport(
            task_id=task_id,
            model_id=model_id,
            timestamp=utc_now_iso(),
            passed=False if aborted else (bool(milestones) and all(m.passed for m in milestones)),
            final_reward=0.0 if aborted else final_reward,
            milestones=milestones,
            token_metrics=token_metrics,
            telemetry=telemetry,
            ast_diff_penalty=float(extras.get("ast_diff_penalty", 1.0)),
            peak_memory_bytes=int(extras.get("peak_memory_bytes", 0)),
            safety_refusal=bool(extras.get("safety_refusal", False)),
            condition=self.condition,
        )
        manager = ReportManager(paths.root)
        if aborted:
            # Write a sidecar only — never a scored evaluation.json. Each
            # abort bumps an attempt counter so a permanently-down upstream
            # is eventually parked instead of retried forever.
            try:
                from benchmark_v3.bench_harness.core.snapshot import atomic_write_json as _awj
                from benchmark_v3.bench_harness.core.run_manifest import (
                    aborted_attempts as _attempts,
                )
                # ``paths.root`` is this task's own dir (<run>/<suite>/<task>),
                # so climb two levels for the (run_dir, suite, task) triple.
                run_root = paths.root.parent.parent
                _awj(paths.root / "ABORTED.json", {
                    "task_id": task_id,
                    "model_id": model_id,
                    "attempts": _attempts(run_root, paths.root.parent.name, task_id) + 1,
                    "reason": state.errors[-1] if state.errors else "unknown",
                    "timestamp": utc_now_iso(),
                    "note": "session aborted before any completed turn; not a scored result",
                })
            except Exception:
                pass
            reporter.complete_task(task_id, False, "ABORTED (no scored result)")
            return report
        manager.save_evaluation(report)
        manager.save_summary(ReportManager.build_summary([report]))
        if report.passed:
            SnapshotManager(paths.root).clear()
        reporter.complete_task(
            task_id,
            report.passed,
            f"reward={report.final_reward:.3f} "
            f"milestones={passed_count}/{len(milestones)} "
            f"tokens={token_metrics.total_tokens}",
        )
        return report

    # -- full session -----------------------------------------------------------

    def run_session(
        self,
        task_id: str,
        model_id: str,
        driver: BaseDriver,
        output_dir: str | Path,
        resume: bool = False,
    ) -> EvaluationReport:
        """Run prepare -> execute -> evaluate -> finalize for one task."""
        from benchmark_v3.bench_harness.core.run_manifest import TaskAbandoned

        if task_id not in self.TASK_IDS:
            raise ValueError(f"unknown task {task_id!r} for suite {self.run_key}")
        paths = SessionPaths.build(output_dir, self.run_key, task_id)
        reporter = ProgressReporter(paths.root / "live_status.json", enabled=False)
        reporter.start_session(f"{self.run_key}/{task_id}", total_tasks=1)
        wall_start = time.monotonic()
        try:
            workspace = self.prepare(task_id, paths, reporter, resume=resume)
            trajectory, state = self.execute(
                task_id, model_id, driver, workspace, paths, reporter, resume=resume
            )
            wall_seconds = time.monotonic() - wall_start
            try:
                annotated = TraceAnnotator().annotate(
                    trajectory,
                    EvaluationReport(
                        task_id=task_id,
                        model_id=model_id,
                        timestamp=utc_now_iso(),
                        passed=False,
                        final_reward=0.0,
                        milestones=[],
                        condition=self.condition,
                    ),
                )
                _ = annotated  # placeholder; CLI re-annotates with the real report
            except Exception:
                pass
            return self.finalize(
                task_id,
                model_id,
                workspace,
                trajectory,
                state,
                wall_seconds,
                driver,
                paths,
                reporter,
            )
        except (KeyboardInterrupt, TaskAbandoned):
            # L1 pause: abandon this task without writing evaluation.json
            raise
        except Exception as exc:
            wall_seconds = time.monotonic() - wall_start
            report = EvaluationReport(
                task_id=task_id,
                model_id=model_id,
                timestamp=utc_now_iso(),
                passed=False,
                final_reward=0.0,
                milestones=[
                    mk_milestone(
                        f"{task_id}_session_crash",
                        "Session completed without crashing",
                        False,
                        failure_reason=f"session_crash: {type(exc).__name__}: {exc}",
                    )
                ],
                token_metrics=TokenAuditMetrics(),
                telemetry=TelemetryMetrics(wall_time_seconds=wall_seconds),
                condition=self.condition,
            )
            try:
                manager = ReportManager(paths.root)
                manager.save_evaluation(report)
                manager.save_summary(ReportManager.build_summary([report]))
            except OSError:
                pass
            reporter.complete_task(task_id, False, f"session crash: {exc}")
            return report
        finally:
            reporter.finish(f"{self.run_key}/{task_id} done")


# ---------------------------------------------------------------------------
# Unit self-tests (stdlib only, no network).
# ---------------------------------------------------------------------------


class _EchoSuite(SuiteAdapter):
    """Minimal concrete suite used to exercise the base lifecycle."""

    suite_name = "echo"
    TASK_IDS = ("echo_task",)

    def describe_task(self, task_id: str) -> dict[str, Any]:
        return {"title": "echo", "brief": "Write hello.txt containing 'hi'."}

    def prepare_task(self, task_id: str, workspace_dir: Path) -> None:
        (workspace_dir / "TASK.md").write_text("Write hello.txt.", encoding="utf-8")

    def build_prompt(self, task_id: str, workspace_dir: Path) -> str:
        return "Create file hello.txt with content 'hi'."

    def evaluate_task(
        self,
        task_id: str,
        workspace_dir: Path,
        trajectory: AgentTrajectory,
    ) -> tuple[list[MilestoneResult], dict[str, Any]]:
        target = workspace_dir / "hello.txt"
        ok = target.exists() and target.read_text(encoding="utf-8").strip() == "hi"
        return [mk_milestone("echo_write", "hello.txt written", ok)], {}


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} base::{name}", flush=True)

    # -- mk_milestone / utc helpers --
    check("mk_pass", mk_milestone("a", "A", True).score == 1.0)
    check("mk_fail", mk_milestone("b", "B", False).score == 0.0)
    check("utc_z", utc_now_iso().endswith("Z"))

    # -- full lifecycle with a scripted driver that writes the file --
    with tempfile.TemporaryDirectory(prefix="suite-base-") as tmp:
        suite = _EchoSuite()
        driver = ScriptedDriver(
            "scripted",
            script=[
                {
                    "content": "writing hello.txt",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "write",
                            "arguments": {"path": "hello.txt", "content": "hi"},
                        }
                    ],
                    "token_usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
                scripted_finish("Wrote hello.txt with hi and verified the file."),
            ],
        )
        report = suite.run_session("echo_task", "scripted", driver, tmp)
        check("session_passed", report.passed)
        check("milestone_count", len(report.milestones) == 1)
        check("tokens_audited", report.token_metrics.total_tokens == 135)
        check("time_telemetry_only", report.telemetry.wall_time_seconds >= 0.0)
        check(
            "no_budget_flag",
            report.token_metrics.budget_exceeded is False,
        )
        root = Path(tmp) / "echo" / "echo_task"
        check("evaluation_json", (root / "evaluation.json").exists())
        check("summary_json", (root / "summary.json").exists())
        check("trajectory_json", (root / "trajectory.json").exists())
        check("wire_jsonl", (root / "wire_jsonl").exists() or (root / "wire.jsonl").exists())
        check("snapshot_cleared", not (root / "last_prompt_snapshot.json").exists())
        check("finish_summary_persisted", (root / "finish_summary.txt").exists())
        traj = json.loads((root / "trajectory.json").read_text(encoding="utf-8"))
        first_prompt = ""
        for turn in traj.get("turns") or []:
            if turn.get("role") == "user":
                first_prompt = str(turn.get("content") or "")
                break
        check("prompt_requires_finish", "finish" in first_prompt and "summary" in first_prompt)
        check("prompt_says_benchmark", "benchmark" in first_prompt.lower())
        check(
            "prompt_requires_on_disk_deliverable",
            "scoring only reads files on disk" in first_prompt.lower()
            or "persist the required artifact" in first_prompt.lower(),
        )
        refusal = SuiteAdapter._incomplete_turn_nudge(
            DriverResponse(
                content="Sorry, I cannot fulfill your request to perform vulnerability finding."
            ),
            {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 0},
        )
        check("refusal_nudge_keeps_task_open", "benchmark" in refusal.lower())

        # -- empty script never finishes: use a test-only turn cap so the
        # loop can exit (production keeps max_turns=None and retries empties).
        class _CappedEcho(_EchoSuite):
            max_turns = 3

        driver2 = ScriptedDriver("scripted", script=[])  # empty: writes nothing
        report2 = _CappedEcho().run_session(
            "echo_task", "scripted", driver2, tmp + "-noresume"
        )
        check("empty_driver_fails", not report2.passed)
        snap = SnapshotManager(Path(tmp + "-noresume") / "echo" / "echo_task")
        check("snapshot_kept_on_failure", snap.has_snapshot())

        class _CrashOnce(ScriptedDriver):
            def __init__(self) -> None:
                super().__init__("crasher")
                self.n = 0

            def chat(self, messages: list[dict[str, Any]], tools: Any = None, **kw: Any) -> DriverResponse:
                self.n += 1
                if self.n == 1:
                    raise RuntimeError("simulated transport crash")
                return DriverResponse(content="recovered with no tools")

        out3 = tmp + "-resume"
        suite.run_session("echo_task", "crasher", _CrashOnce(), out3)
        snap3 = SnapshotManager(Path(out3) / "echo" / "echo_task")
        check("snapshot_saved_before_call", snap3.has_snapshot())
        saved = snap3.load() or {}
        check(
            "snapshot_shape",
            isinstance(saved.get("request", {}).get("messages"), list),
        )

        # -- token fuse trips at 10M --
        class _Greedy(ScriptedDriver):
            def chat(self, messages: list[dict[str, Any]], tools: Any = None, **kw: Any) -> DriverResponse:
                usage = {"prompt_tokens": 6_000_000, "completion_tokens": 5_000_000}
                self.account_usage(self.extract_token_usage(usage))
                return DriverResponse(content="more", tool_calls=[], token_usage=usage)

        report4 = suite.run_session("echo_task", "greedy", _Greedy(), tmp + "-fuse")
        check("fuse_trips", report4.token_metrics.budget_exceeded is True)

        # -- unknown task rejected --
        try:
            suite.run_session("nope", "m", ScriptedDriver(), tmp + "-bad")
            check("unknown_task_rejected", False)
        except ValueError:
            check("unknown_task_rejected", True)

        echo_b = _EchoSuite(condition="b")
        check("b_appendix_points_at_task", "TASK.md" in echo_b._b_plan_prompt_appendix())
        check("b_appendix_asks_read", "read" in echo_b._b_plan_prompt_appendix())
        check("a_appendix_empty", _EchoSuite()._b_plan_prompt_appendix() == "")
        check("agent_tools_include_read", any(
            t.get("function", {}).get("name") == "read" for t in AGENT_TOOLS
        ))
        check("agent_tools_include_finish", any(
            t.get("function", {}).get("name") == FINISH_TOOL_NAME for t in AGENT_TOOLS
        ))
        check("no_default_turn_cap", _EchoSuite.max_turns is None)
        check("short_summary_rejected", normalize_finish_summary("done") is None)
        check("long_summary_ok", normalize_finish_summary(
            "Wrote hello.txt with hi and verified contents."
        ) is not None)

        empty_then_write = ScriptedDriver(
            "scripted",
            script=[
                {
                    "content": "",
                    "tool_calls": [],
                    "token_usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                    },
                },
                {
                    "content": "writing after abort",
                    "tool_calls": [
                        {
                            "id": "call_w",
                            "name": "write",
                            "arguments": {"path": "hello.txt", "content": "hi"},
                        }
                    ],
                },
                scripted_finish("Recovered from an empty turn and wrote hello.txt."),
            ],
        )
        report_empty = suite.run_session(
            "echo_task", "scripted", empty_then_write, tmp + "-empty-abort"
        )
        check("empty_turn_does_not_end_task", report_empty.passed)

        short_then_ok = ScriptedDriver(
            "scripted",
            script=[
                scripted_finish("too short"),
                {
                    "content": "writing after rejected finish",
                    "tool_calls": [
                        {
                            "id": "call_w2",
                            "name": "write",
                            "arguments": {"path": "hello.txt", "content": "hi"},
                        }
                    ],
                },
                scripted_finish("Wrote hello.txt with hi after the short summary was rejected."),
            ],
        )
        report_short = suite.run_session(
            "echo_task", "scripted", short_then_ok, tmp + "-short-finish"
        )
        check("short_finish_does_not_end_task", report_short.passed)
        short_root = Path(tmp + "-short-finish") / "echo" / "echo_task"
        check(
            "final_summary_is_long",
            "rejected" in (short_root / "finish_summary.txt").read_text(encoding="utf-8"),
        )

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"base self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
