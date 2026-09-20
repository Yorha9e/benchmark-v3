"""Secure subprocess invocation with timeout and cross-platform tree kill."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "RunResult",
    "ProcessRunner",
    "bind_to_kill_on_close_job",
    "close_job_handle",
    "kill_process_tree",
]

# ---------------------------------------------------------------------------
# Windows Job Object (kill-on-job-close) support
# ---------------------------------------------------------------------------
#
# Children spawned for model-authored code (probes, node processes) can leak
# grandchildren (node workers, npm helpers) that outlive the parent. Binding
# a child to a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes the
# kernel reap the whole tree the moment the parent closes the job handle —
# including when the harness itself dies. Fail-open everywhere: if the Win32
# APIs are unavailable (non-Windows, odd Python builds) every helper is a
# harmless no-op.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFO_SIZE = 144  # sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) on Win64
_JOB_HANDLE_NONE = 0


def _kernel32():  # noqa: ANN202 - returns windll or None
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if not hasattr(kernel32, "CreateJobObjectW"):
        return None
    return kernel32


def bind_to_kill_on_close_job(pid: int) -> int:
    """Assign *pid* to a fresh Job Object with KILL_ON_JOB_CLOSE.

    Returns the job handle (truthy int) for a later :func:`close_job_handle`,
    or 0 when the binding is unavailable (POSIX / API failure) — callers must
    treat 0 as "no job", never as a valid handle. Binding never raises.
    """
    if sys.platform != "win32" or pid <= 0:
        return _JOB_HANDLE_NONE
    kernel32 = _kernel32()
    if kernel32 is None:
        return _JOB_HANDLE_NONE
    try:
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return _JOB_HANDLE_NONE
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", ctypes.c_uint),
                ("SchedulingClass", ctypes.c_uint),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(job),
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            _JOB_OBJECT_EXTENDED_LIMIT_INFO_SIZE,
        )
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return _JOB_HANDLE_NONE
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not handle:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return _JOB_HANDLE_NONE
        try:
            if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle)):
                kernel32.CloseHandle(ctypes.c_void_p(job))
                return _JOB_HANDLE_NONE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        return int(job)
    except Exception:  # fail-open: isolation aid only, never break the caller
        return _JOB_HANDLE_NONE


def close_job_handle(job: int) -> None:
    """Close a job handle from :func:`bind_to_kill_on_close_job`.

    On Windows, closing the last job handle reaps any processes still in the
    job (KILL_ON_JOB_CLOSE). No-op for handle 0 / non-Windows / closed jobs.
    """
    if not job or sys.platform != "win32":
        return
    kernel32 = _kernel32()
    if kernel32 is None:
        return
    try:
        kernel32.CloseHandle(ctypes.c_void_p(job))
    except Exception:
        pass


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
            "encoding": "utf-8",
            "errors": "replace",
            "shell": shell,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        started = time.monotonic()
        job_handle = 0
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
        # Windows: bind the child tree to a kill-on-close Job Object so any
        # grandchildren it leaks are reaped when this runner finishes.
        job_handle = bind_to_kill_on_close_job(proc.pid)
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
        finally:
            # Closing the job handle reaps any survivors (KILL_ON_JOB_CLOSE).
            close_job_handle(job_handle)
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
        # 强制 Windows 子进程标准输出采用 UTF-8 编码，防止中文环境下默认 ANSI/CP936 造成乱码
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if extra_env:
            env.update(extra_env)
        return env
