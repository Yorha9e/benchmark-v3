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
    "ScriptedDriver",
    "SessionPaths",
    "SuiteAdapter",
    "mk_milestone",
    "utc_now_iso",
]


#: Hard workspace-boundary notice appended to every suite's initial prompt.
#: Mirrors the static :func:`_command_escapes_workspace` screen so compliant
#: models do not burn limited turns on refused calls.
WORKSPACE_BOUNDARY_NOTICE = (
    "\n\nWorkspace boundary (hard rule): you may ONLY read, write, and run "
    "commands inside the current working directory. Paths containing `..`, "
    "absolute paths (e.g. `/etc/...`, `C:\\...`), home-directory references "
    "(`~`, `$HOME`), and `file:` URIs are refused with an ERROR and waste one "
    "of your limited turns. The task is fully self-contained — there is "
    "nothing useful outside the workspace, do not look for it."
)


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


#: Shell metacharacter-adjacent ``..`` segments: ``../``, ``..\``, bare
#: ``..`` as a path component (``cd ..``, ``cat ../x``). Matched
#: case-insensitively against the raw command string.
_PARENT_TRAVERSAL_RE = None  # lazy-compiled (see _command_escapes_workspace)


def _command_escapes_workspace(command: str) -> str | None:
    """Best-effort static screen: does this shell command reach outside cwd?

    Each ``bash`` tool call runs as a fresh, short-lived shell rooted at the
    task workspace, so per-command screening is sufficient (no persistent
    ``cd`` state survives between calls). Returns a human-readable reason
    when the command must be refused, else ``None``.

    Blocked: parent traversal (``..``), absolute POSIX paths, Windows
    drive/UNC paths, home-directory expansion (``~``, ``$HOME``), and
    well-known env-var indirection out of the workspace. This is a
    defense-in-depth screen, not a shell sandbox: exotic bypasses
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
    lowered = text.lower()
    for marker in ("$home", "${home}", "$userprofile", "%userprofile%", "%home%"):
        if marker in lowered:
            return f"home-directory reference ({marker}) is not allowed; stay inside the workspace"
    if re.search(r"\b(HOME|USERPROFILE)\b", text):
        return "home-directory reference (HOME/USERPROFILE) is not allowed; stay inside the workspace"
    # Git escape hatches: re-pointing git at the harness repo would bypass the
    # GIT_CEILING_DIRECTORIES isolation injected at execution time.
    if re.search(r"\bgit_ceiling_directories\b|\bgit_dir\b|--git-dir|--work-tree", lowered):
        return "git repository overrides are not allowed inside the workspace"
    # `/dev/null`  drains are harmless and idiomatic; exempt before screening.
    screened = re.sub(r"/dev/null\b", "", text)
    # Absolute POSIX path, home expansion, or redirect target outside cwd.
    if re.search(r"(^|[\s;|&`$()'\"=><])(~|/)(?=$|[\s;|&`$()'\"=><]|[\w.~\/])", screened):
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
]


class ScriptedDriver(BaseDriver):
    """Deterministic offline driver replaying a canned response script.

    Each script entry is a ``dict`` with optional ``content``, ``thought``,
    ``tool_calls`` (``[{id?, name, arguments}]``) and ``token_usage``.
    When the script is exhausted, an empty terminal response is returned.
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
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens + self.reasoning_tokens


class SuiteAdapter(ABC):
    """Abstract base for the four benchmark suites.

    Subclasses provide task content by implementing :meth:`prepare_task`,
    :meth:`build_prompt` and :meth:`evaluate_task`; this base class owns
    the whole session lifecycle, token fuse, telemetry, snapshots,
    tracing and persistence.
    """

    #: Short suite key used in output paths and the CLI registry.
    suite_name: str = "base"
    #: Ordered task ids of this suite.
    TASK_IDS: tuple[str, ...] = ()
    #: Max agent turns per session (bounds cost even before the token fuse).
    max_turns: int = 12

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
        workspace.record_baseline()
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
        session_id = f"{self.suite_name}-{task_id}-{uuid.uuid4().hex[:8]}"
        collector = TraceCollector(wire_path=paths.wire_path)
        collector.start_session(session_id, task_id, model_id)
        snapshot = SnapshotManager(paths.root)
        runner = ProcessRunner(default_timeout=60.0)

        prompt = self.build_prompt(task_id, workspace.workspace_dir)
        prompt = prompt + WORKSPACE_BOUNDARY_NOTICE
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        collector.record_user_turn(prompt)

        state = _LoopState()
        wall_start = time.monotonic()
        start_turn = 0
        if resume:
            saved = snapshot.load()
            if saved and isinstance(saved.get("request"), dict):
                request = saved["request"]
                if isinstance(request.get("messages"), list) and request["messages"]:
                    messages = request["messages"]
                try:
                    start_turn = int(saved.get("turn_index", 0))
                except (TypeError, ValueError):
                    start_turn = 0
                collector.load_existing_messages(messages)
                reporter.update(task_id, "retry", f"resuming at turn {start_turn} with full context ({len(messages)} msgs)")

        for turn in range(start_turn, self.max_turns):
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
                break  # model finished (final content recorded above)

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
            reporter.update(
                task_id,
                "running",
                f"turn {turn}: {state.tools_executed} tool calls",
                turn=turn,
                tokens=state.total,
            )

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
        report = EvaluationReport(
            task_id=task_id,
            model_id=model_id,
            timestamp=utc_now_iso(),
            passed=bool(milestones) and all(m.passed for m in milestones),
            final_reward=final_reward,
            milestones=milestones,
            token_metrics=token_metrics,
            telemetry=telemetry,
            ast_diff_penalty=float(extras.get("ast_diff_penalty", 1.0)),
            peak_memory_bytes=int(extras.get("peak_memory_bytes", 0)),
            safety_refusal=bool(extras.get("safety_refusal", False)),
        )
        manager = ReportManager(paths.root)
        manager.save_evaluation(report)
        manager.save_summary(ReportManager.build_summary([report]))
        if report.passed:
            SnapshotManager(workspace.workspace_dir).clear()
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
        if task_id not in self.TASK_IDS:
            raise ValueError(f"unknown task {task_id!r} for suite {self.suite_name}")
        paths = SessionPaths.build(output_dir, self.suite_name, task_id)
        reporter = ProgressReporter(paths.root / "live_status.json", enabled=False)
        reporter.start_session(f"{self.suite_name}/{task_id}", total_tasks=1)
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
            reporter.finish(f"{self.suite_name}/{task_id} done")


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
                {"content": "done"},
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
        check("snapshot_cleared", not (root / "workspace" / "last_prompt_snapshot.json").exists())

        # -- resume replay: crash mid-loop, then resume re-issues one turn --
        driver2 = ScriptedDriver("scripted", script=[])  # empty: writes nothing
        report2 = suite.run_session("echo_task", "scripted", driver2, tmp + "-noresume")
        check("empty_driver_fails", not report2.passed)
        snap = SnapshotManager(Path(tmp + "-noresume") / "echo" / "echo_task" / "workspace")
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
        snap3 = SnapshotManager(Path(out3) / "echo" / "echo_task" / "workspace")
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

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"base self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
