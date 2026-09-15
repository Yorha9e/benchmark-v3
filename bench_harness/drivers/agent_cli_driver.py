"""External black-box agent CLI driver (Kimi Code CLI / Aider, ...).

Runs an agent CLI as a subprocess via :class:`ProcessRunner`, streams
stdout/stderr, and parses ``[TOOL:name {json-args}]`` markers into
structured tool calls. Plain output becomes the response content.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from benchmark_v3.bench_harness.core.runner import ProcessRunner
from benchmark_v3.bench_harness.drivers.base import (
    BaseDriver,
    DriverResponse,
    PermanentDriverError,
)

TOOL_MARKER_RE = re.compile(r"\[TOOL:(?P<name>[A-Za-z0-9_\-]+)\s*(?P<args>\{.*?\})?\]", re.DOTALL)


class AgentCLIDriver(BaseDriver):
    """Subprocess driver for external agent CLIs.

    Parameters
    ----------
    model_id:
        Label identifying the external agent (e.g. ``"kimi-cli"``).
    cli_command:
        Base argv of the CLI, e.g. ``["kimi", "agent"]`` or ``["aider"]``.
    cwd:
        Working directory (workspace) for the CLI process.
    timeout:
        Per-turn subprocess timeout in seconds.
    runner:
        Injected :class:`ProcessRunner` (created by default).
    """

    # CLI subprocesses manage their own retries; harness retry stays minimal.
    def __init__(
        self,
        model_id: str,
        cli_command: list[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float = 300.0,
        runner: ProcessRunner | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("max_retries", 1)
        super().__init__(model_id, **kwargs)
        self.cli_command = list(cli_command or ["agent-cli"])
        self.cwd = cwd
        self.timeout = timeout
        self.runner = runner or ProcessRunner(default_timeout=timeout)

    @staticmethod
    def build_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> str:
        """Flatten chat history into a single CLI prompt string."""
        lines: list[str] = []
        if tools:
            names = []
            for tool_def in tools:
                fn = tool_def.get("function", tool_def)
                names.append(fn.get("name", "?"))
            lines.append(f"Available tools: {', '.join(names)}")
            lines.append("Emit tool calls as markers like: [TOOL:name {\"arg\": \"value\"}]")
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    @classmethod
    def parse_cli_output(cls, output: str) -> tuple[str, list[dict[str, Any]]]:
        """Split CLI stdout into ``(content, tool_calls)`` via markers."""
        tool_calls: list[dict[str, Any]] = []
        for index, match in enumerate(TOOL_MARKER_RE.finditer(output)):
            name = match.group("name")
            raw_args = match.group("args") or "{}"
            try:
                arguments = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                arguments = {"_raw": raw_args}
            tool_calls.append({"id": f"cli_call_{index}", "name": name, "arguments": arguments})
        content = TOOL_MARKER_RE.sub("", output).strip()
        return content, tool_calls

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_stream: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> DriverResponse:
        prompt = self.build_prompt(messages, tools)
        argv = [*self.cli_command, *kwargs.get("extra_args", []), prompt]

        def _stream(stdout: str, stderr: str) -> None:
            if on_stream is not None:
                if stdout:
                    on_stream(stdout)
                if stderr:
                    on_stream(stderr)

        result = self.runner.run(argv, timeout=kwargs.get("timeout", self.timeout), cwd=self.cwd, on_output=_stream)
        if result.timed_out:
            raise PermanentDriverError(f"agent CLI timed out after {self.timeout}s: {' '.join(argv[:3])}")
        if result.returncode == 127:
            raise PermanentDriverError(f"agent CLI not found: {result.stderr.strip()}")
        if result.returncode != 0 and not result.stdout.strip():
            raise PermanentDriverError(f"agent CLI failed (exit={result.returncode}): {result.stderr.strip()[:500]}")
        content, raw_calls = self.parse_cli_output(result.stdout)
        response = DriverResponse(
            content=content or result.stdout.strip(),
            thought="",
            tool_calls=self.normalize_tool_calls(raw_calls),
            token_usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0},
        )
        return response
