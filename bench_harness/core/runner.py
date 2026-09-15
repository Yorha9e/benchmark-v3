"""Secure subprocess invocation with timeout and cross-platform tree kill."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field


@dataclass
class RunResult:
    """Outcome of a single subprocess invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0

    def to_dict(self) -> dict:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_s": self.duration_s,
            "argv": list(self.argv),
        }


def kill_process_tree(pid: int) -> None:
    """Terminate a whole process tree rooted at ``pid``, cross-platform.

    Windows uses ``taskkill /F /T /PID``; POSIX sends SIGKILL to the
    process group created via ``start_new_session=True``.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError):
            pass


class ProcessRunner:
    """Subprocess runner with timeout enforcement and env isolation.

    Parameters
    ----------
    default_timeout:
        Fallback timeout in seconds for :meth:`run`.
    default_cwd:
        Default working directory for child processes.
    inherit_env:
        When False, the child gets a minimal scrubbed environment plus
        ``extra_env`` (PATH/SystemRoot preserved for usability).
    """

    def __init__(
        self,
        default_timeout: float = 60.0,
        default_cwd: str | os.PathLike[str] | None = None,
        inherit_env: bool = True,
    ) -> None:
        self.default_timeout = default_timeout
        self.default_cwd = default_cwd
        self.inherit_env = inherit_env

    def run(
        self,
        argv: Sequence[str] | str,
        timeout: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        shell: bool = False,
        on_output: Callable[[str, str], None] | None = None,
    ) -> RunResult:
        """Run a command, enforcing ``timeout`` via process-tree kill."""
        if isinstance(argv, str) and not shell:
            argv = argv.split()
        display_argv = list(argv) if isinstance(argv, (list, tuple)) else [str(argv)]
        deadline = timeout if timeout is not None else self.default_timeout
        workdir = cwd if cwd is not None else self.default_cwd
        env = self._build_env(extra_env)

        popen_kwargs: dict = {
            "cwd": workdir,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "shell": shell,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        started = time.monotonic()
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 — caller-owned harness input
        except OSError as exc:
            return RunResult(
                returncode=127,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                duration_s=0.0,
                argv=display_argv,
            )
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=deadline)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            timed_out = True
        duration = time.monotonic() - started
        result = RunResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=timed_out,
            duration_s=duration,
            argv=display_argv,
        )
        if on_output is not None:
            on_output(result.stdout, result.stderr)
        return result

    def _build_env(self, extra_env: Mapping[str, str] | None) -> dict[str, str] | None:
        if self.inherit_env:
            env = dict(os.environ)
        else:
            env = {}
            for key in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG", "LC_ALL"):
                if key in os.environ:
                    env[key] = os.environ[key]
        if extra_env:
            env.update(extra_env)
        return env
