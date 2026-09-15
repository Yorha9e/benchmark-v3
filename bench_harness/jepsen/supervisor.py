"""Multi-node process lifecycle supervisor with external hard SIGKILL.

Covers ``benchmark_v3/bench_harness/jepsen/supervisor.py``:

* :class:`NodeProcess` — spawn / health-check / restart / graceful-stop for one
  simulated distributed-system node. The node's working directory and log file
  are **preserved across crashes and restarts** so state-recovery tests observe
  the same on-disk state a real crash would leave behind.
* :func:`hard_kill_pid` — external, unfriendly process-tree kill:

  - Windows: ``taskkill /F /T /PID <pid>``
  - POSIX: ``os.killpg(os.getpgid(pid), SIGKILL)`` with an
    ``os.kill(pid, SIGKILL)`` fallback.
* :class:`MarkerWatcher` — tails a node's log/status file and fires a callback
  the instant a marker line (e.g. ``CRITICAL_WRITE_POINT``) appears, so a fault
  (typically an instant SIGKILL) lands at the exact critical moment.
* :class:`SupervisorManager` — owns a set of :class:`NodeProcess` instances and
  their marker watchers; usable as a context manager.

Standard library only, cross-platform (Windows + POSIX).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "CRITICAL_WRITE_MARKER",
    "MarkerWatcher",
    "NodeProcess",
    "NodeState",
    "NodeStatus",
    "SupervisorManager",
    "hard_kill_pid",
]

#: Default log marker that denotes the instant-before-commit critical section.
CRITICAL_WRITE_MARKER = "CRITICAL_WRITE_POINT"

_DEVNULL = subprocess.DEVNULL


class NodeState(str, Enum):
    """Lifecycle state of a supervised node process."""

    PENDING = "pending"  #: never spawned (or spawn failed)
    RUNNING = "running"  #: last known alive
    STOPPED = "stopped"  #: exited cleanly (return code 0)
    CRASHED = "crashed"  #: exited with a non-zero code / signal
    KILLED = "killed"  #: terminated via :meth:`NodeProcess.hard_kill`


@dataclass
class NodeStatus:
    """Point-in-time health snapshot of one node."""

    node_id: str
    state: NodeState
    pid: int | None = None
    exit_code: int | None = None
    restarts: int = 0
    workdir: str = ""
    last_heartbeat: float = 0.0


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* currently exists (no side effects)."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # NOTE: OpenProcess alone is not a liveness check on Windows —
            # the kernel object survives termination while any handle (e.g.
            # our own Popen handle) is still open. Compare the exit code
            # against STILL_ACTIVE instead.
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but not ours
    except OSError:
        return False
    return True


def hard_kill_pid(pid: int, timeout: float = 10.0) -> bool:
    """Externally SIGKILL the process tree rooted at *pid*.

    Windows uses ``taskkill /F /T /PID <pid>`` (``/T`` kills the whole tree);
    POSIX uses ``os.killpg(os.getpgid(pid), signal.SIGKILL)`` and falls back to
    ``os.kill(pid, signal.SIGKILL)`` when the process has no process group.

    Blocks until *pid* is gone or *timeout* seconds elapse. Returns True when
    *pid* no longer exists (including "was already gone").
    """
    if pid is None or pid <= 0:
        return False
    if not _pid_alive(pid):
        return True
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=_DEVNULL,
                stderr=_DEVNULL,
                timeout=timeout,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    return not _pid_alive(pid)
                except OSError:
                    return False
    except Exception:
        return not _pid_alive(pid)
    deadline = time.monotonic() + max(timeout, 0.0)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _popen_detached_kwargs() -> dict[str, Any]:
    """Popen kwargs that detach the child into its own process group."""
    if os.name == "nt":
        flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flag} if flag else {}
    return {"start_new_session": True}


class NodeProcess:
    """Lifecycle wrapper around one simulated node subprocess.

    Parameters
    ----------
    node_id:
        Stable logical id (e.g. ``"n1"``). Used for the default log name.
    command:
        Program to run — a ``[argv...]`` sequence (recommended) or a shell
        string (then executed with ``shell=True``).
    workdir:
        State directory for the node. Created on demand and **never deleted**
        by this class, so files survive crashes and restarts.
    log_path:
        Where stdout/stderr are appended. Defaults to
        ``<workdir>/node-<node_id>.log``.
    env:
        Extra environment variables merged over ``os.environ``.
    """

    def __init__(
        self,
        node_id: str,
        command: Sequence[str] | str,
        workdir: str | os.PathLike[str],
        log_path: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        popen_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.node_id = str(node_id)
        self.command = command
        self.workdir = str(workdir)
        Path(self.workdir).mkdir(parents=True, exist_ok=True)
        if log_path is None:
            log_path = str(Path(self.workdir) / f"node-{self.node_id}.log")
        self.log_path = str(log_path)
        self.env = dict(env) if env else {}
        self._extra_popen_kwargs = dict(popen_kwargs) if popen_kwargs else {}
        self._proc: subprocess.Popen[str] | None = None
        self._state = NodeState.PENDING
        self._exit_code: int | None = None
        self.restarts = 0
        self._lock = threading.Lock()

    # -- introspection ----------------------------------------------------
    @property
    def state(self) -> NodeState:
        return self._state

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def is_alive(self) -> bool:
        """Return True if the child process is currently running."""
        proc = self._proc
        return proc is not None and proc.poll() is None

    def status(self) -> NodeStatus:
        """Return a :class:`NodeStatus` snapshot (refreshes exit state)."""
        return self.health_check()

    # -- lifecycle --------------------------------------------------------
    def spawn(self) -> int:
        """Start the node process. Raises RuntimeError if already running."""
        with self._lock:
            if self.is_alive():
                raise RuntimeError(f"node {self.node_id} is already running")
            Path(self.workdir).mkdir(parents=True, exist_ok=True)
            merged_env = dict(os.environ)
            merged_env.update(self.env)
            kwargs: dict[str, Any] = dict(_popen_detached_kwargs())
            kwargs.update(self._extra_popen_kwargs)
            use_shell = isinstance(self.command, str)
            # Opened in append mode so logs survive restarts; the parent copy
            # is closed right after spawn (the child keeps its own dup).
            with open(self.log_path, "a", encoding="utf-8") as log_fh:
                proc = subprocess.Popen(
                    self.command,
                    shell=use_shell,
                    cwd=self.workdir,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=merged_env,
                    text=True,
                    **kwargs,
                )
            self._proc = proc
            self._exit_code = None
            self._state = NodeState.RUNNING
            return proc.pid

    def health_check(self) -> NodeStatus:
        """Poll the child; fold an observed exit into STOPPED/CRASHED."""
        with self._lock:
            proc = self._proc
            pid = proc.pid if proc is not None else None
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    self._state = NodeState.RUNNING
                elif self._state == NodeState.RUNNING:
                    # Only reinterpret a *fresh* exit; an explicit hard_kill()
                    # already stamped KILLED and must not be overwritten.
                    self._exit_code = rc
                    self._state = (
                        NodeState.STOPPED if rc == 0 else NodeState.CRASHED
                    )
            return NodeStatus(
                node_id=self.node_id,
                state=self._state,
                pid=pid,
                exit_code=self._exit_code,
                restarts=self.restarts,
                workdir=self.workdir,
                last_heartbeat=time.time(),
            )

    def graceful_stop(self, timeout: float = 10.0) -> bool:
        """SIGTERM (TerminateProcess on Windows), wait, else escalate to kill."""
        with self._lock:
            proc = self._proc
            if proc is None:
                if self._state == NodeState.RUNNING:
                    self._state = NodeState.STOPPED
                return True
            if proc.poll() is not None:
                self._sync_exit_locked(proc)
                return True
            proc.terminate()
            try:
                rc = proc.wait(timeout=max(timeout, 0.0))
            except subprocess.TimeoutExpired:
                pid = proc.pid
                # Release the lock while killing to avoid blocking watchers.
                self._lock.release()
                try:
                    hard_kill_pid(pid, timeout=timeout)
                finally:
                    self._lock.acquire()
                rc = proc.wait(timeout=timeout)
                self._exit_code = rc
                self._state = NodeState.KILLED
                return proc.poll() is not None
            # A deliberate graceful stop is STOPPED by definition, even though
            # terminate() surfaces as rc=1 (Windows) or -SIGTERM (POSIX).
            self._exit_code = rc
            self._state = NodeState.STOPPED
            return True

    def hard_kill(self, timeout: float = 10.0) -> bool:
        """External unfriendly kill of the whole process tree. Never graceful."""
        with self._lock:
            proc = self._proc
            if proc is None:
                self._state = NodeState.KILLED
                return True
            if proc.poll() is not None:
                self._exit_code = proc.returncode
                self._state = NodeState.KILLED
                return True
            pid = proc.pid
            self._lock.release()
            try:
                dead = hard_kill_pid(pid, timeout=timeout)
            finally:
                self._lock.acquire()
            try:
                self._exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
            self._state = NodeState.KILLED
            return dead and proc.poll() is not None

    def restart(self, graceful: bool = True, stop_timeout: float = 10.0) -> int:
        """Stop (gracefully or hard) and spawn again.

        The working directory, log file and env are preserved, so on-disk
        state carries over exactly like a real crash/reboot cycle.
        Returns the new pid.
        """
        if self.is_alive():
            if graceful:
                self.graceful_stop(timeout=stop_timeout)
            else:
                self.hard_kill(timeout=stop_timeout)
        # Break out of the state left by stop/kill back into a fresh spawn.
        pid = self.spawn()
        self.restarts += 1
        return pid

    # -- internals --------------------------------------------------------
    def _sync_exit_locked(
        self, proc: subprocess.Popen[str], rc: int | None = None
    ) -> None:
        if rc is None:
            rc = proc.returncode
        self._exit_code = rc
        if self._state == NodeState.RUNNING:
            self._state = NodeState.STOPPED if rc == 0 else NodeState.CRASHED


MarkerCallback = Callable[[str, str, str], None]
"""Signature ``callback(node_id, marker, matched_line)``."""


class MarkerWatcher(threading.Thread):
    """Tail a log/status file; fire *callback* the instant *marker* appears.

    The file is polled from the current end (or from the start when
    ``from_start=True``). Truncation/recreation (log rotation, node restart)
    resets the read offset automatically. Runs as a daemon thread; call
    :meth:`stop` to shut it down deterministically.
    """

    def __init__(
        self,
        node_id: str,
        path: str | os.PathLike[str],
        marker: str = CRITICAL_WRITE_MARKER,
        callback: MarkerCallback | None = None,
        poll_interval: float = 0.02,
        trigger_once: bool = True,
        from_start: bool = False,
    ) -> None:
        super().__init__(daemon=True, name=f"marker-watcher-{node_id}")
        self.node_id = str(node_id)
        self.path = str(path)
        self.marker = marker
        self.callback = callback
        self.poll_interval = max(poll_interval, 0.001)
        self.trigger_once = trigger_once
        self.from_start = from_start
        self._stop_event = threading.Event()
        self.fired_count = 0
        self.last_error: str | None = None

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the watcher thread to exit and join it."""
        self._stop_event.set()
        self.join(timeout=max(timeout, 0.0))

    def run(self) -> None:  # noqa: C901 - linear poll loop
        offset = 0
        if not self.from_start:
            try:
                offset = os.path.getsize(self.path)
            except OSError:
                offset = 0
        while not self._stop_event.is_set():
            try:
                try:
                    size = os.path.getsize(self.path)
                except OSError:
                    self._stop_event.wait(self.poll_interval)
                    continue
                if size < offset:
                    offset = 0  # truncated / rotated / recreated
                if size > offset:
                    with open(self.path, "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read(size - offset)
                    offset = size
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        if self.marker in line:
                            self.fired_count += 1
                            if self.callback is not None:
                                try:
                                    self.callback(self.node_id, self.marker, line)
                                except Exception as exc:  # never kill watcher
                                    self.last_error = f"{type(exc).__name__}: {exc}"
                            if self.trigger_once:
                                return
            except Exception as exc:  # defensive: keep watching
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop_event.wait(self.poll_interval)


class SupervisorManager:
    """Owns a set of :class:`NodeProcess` nodes plus their marker watchers."""

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        self.base_dir = str(base_dir) if base_dir is not None else os.getcwd()
        self._nodes: dict[str, NodeProcess] = {}
        self._watchers: list[MarkerWatcher] = []
        self._lock = threading.Lock()

    # -- node registry ----------------------------------------------------
    def add_node(self, node: NodeProcess) -> NodeProcess:
        """Register an existing :class:`NodeProcess` (replaces same id)."""
        with self._lock:
            self._nodes[node.node_id] = node
        return node

    def spawn_node(
        self,
        node_id: str,
        command: Sequence[str] | str,
        workdir: str | os.PathLike[str] | None = None,
        **kwargs: Any,
    ) -> NodeProcess:
        """Create, register and spawn a node. Returns the :class:`NodeProcess`."""
        if workdir is None:
            workdir = str(Path(self.base_dir) / f"node-{node_id}")
        node = NodeProcess(node_id, command, workdir, **kwargs)
        with self._lock:
            self._nodes[node_id] = node
        node.spawn()
        return node

    def get(self, node_id: str) -> NodeProcess:
        with self._lock:
            try:
                return self._nodes[node_id]
            except KeyError:
                raise KeyError(f"unknown node: {node_id}") from None

    def node_ids(self) -> list[str]:
        with self._lock:
            return list(self._nodes)

    def spawn_all(self) -> dict[str, int]:
        """Spawn every registered node that is not already running."""
        with self._lock:
            nodes = list(self._nodes.values())
        pids: dict[str, int] = {}
        for node in nodes:
            if not node.is_alive():
                pids[node.node_id] = node.spawn()
            elif node.pid is not None:
                pids[node.node_id] = node.pid
        return pids

    # -- health / control -------------------------------------------------
    def statuses(self) -> dict[str, NodeStatus]:
        with self._lock:
            nodes = list(self._nodes.values())
        return {node.node_id: node.health_check() for node in nodes}

    def any_crashed(self) -> bool:
        return any(
            s.state in (NodeState.CRASHED, NodeState.KILLED)
            for s in self.statuses().values()
        )

    def stop_node(self, node_id: str, timeout: float = 10.0) -> bool:
        return self.get(node_id).graceful_stop(timeout=timeout)

    def kill_node(self, node_id: str, timeout: float = 10.0) -> bool:
        """Hard external SIGKILL of one node (whole process tree)."""
        return self.get(node_id).hard_kill(timeout=timeout)

    def restart_node(
        self,
        node_id: str,
        graceful: bool = True,
        stop_timeout: float = 10.0,
    ) -> int:
        """Restart one node, preserving its workdir/state. Returns new pid."""
        return self.get(node_id).restart(
            graceful=graceful, stop_timeout=stop_timeout
        )

    def stop_all(self, timeout: float = 10.0) -> dict[str, bool]:
        with self._lock:
            nodes = list(self._nodes.values())
        return {node.node_id: node.graceful_stop(timeout=timeout) for node in nodes}

    # -- marker-triggered faults ------------------------------------------
    def watch_marker(
        self,
        node_id: str,
        marker: str = CRITICAL_WRITE_MARKER,
        action: str = "kill",
        callback: MarkerCallback | None = None,
        **watcher_kwargs: Any,
    ) -> MarkerWatcher:
        """Watch *node_id*'s log for *marker*; on match, apply *action*.

        ``action="kill"`` (default) performs an instant hard SIGKILL of the
        node — the canonical "kill at the critical write point" Jepsen trick.
        ``action="none"`` only records; a custom *callback* may be supplied
        instead (it runs in addition to the built-in action).
        """
        if action not in ("kill", "none"):
            raise ValueError(f"unknown marker action: {action!r}")
        node = self.get(node_id)

        def _on_marker(nid: str, hit: str, line: str) -> None:
            if callback is not None:
                callback(nid, hit, line)
            if action == "kill":
                try:
                    node.hard_kill()
                except Exception:
                    pass

        watcher = MarkerWatcher(
            node_id, node.log_path, marker, _on_marker, **watcher_kwargs
        )
        with self._lock:
            self._watchers.append(watcher)
        watcher.start()
        return watcher

    def stop_watchers(self, timeout: float = 5.0) -> None:
        with self._lock:
            watchers = list(self._watchers)
            self._watchers.clear()
        for watcher in watchers:
            watcher.stop(timeout=timeout)

    # -- teardown ---------------------------------------------------------
    def shutdown(self, timeout: float = 10.0) -> dict[str, bool]:
        """Stop watchers, then gracefully stop all nodes. Returns stop map."""
        self.stop_watchers()
        return self.stop_all(timeout=timeout)

    def __enter__(self) -> SupervisorManager:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()


# ---------------------------------------------------------------------------
# Unit self-tests (stdlib only; safe on Windows + POSIX, no network).
# ---------------------------------------------------------------------------

_SLEEP_PROG = "import time; time.sleep(60)"


def self_test() -> tuple[int, int]:
    """Run module self-tests. Returns ``(passed, failed)`` counts."""
    import tempfile

    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} supervisor::{name}", flush=True)

    with tempfile.TemporaryDirectory(prefix="jepsen-sup-") as tmp:
        # 1. spawn + health + graceful stop.
        node = NodeProcess("n1", [sys.executable, "-c", _SLEEP_PROG], f"{tmp}/n1")
        pid = node.spawn()
        check("spawn_returns_pid", isinstance(pid, int) and pid > 0)
        check("health_running", node.health_check().state == NodeState.RUNNING)
        check("is_alive", node.is_alive())
        check("graceful_stop", node.graceful_stop(timeout=10.0) is True)
        check("stopped_state", node.health_check().state == NodeState.STOPPED)

        # 2. hard kill of a live process tree.
        killer = NodeProcess("n2", [sys.executable, "-c", _SLEEP_PROG], f"{tmp}/n2")
        killer.spawn()
        check("hard_kill", killer.hard_kill(timeout=15.0) is True)
        check("killed_state", killer.health_check().state == NodeState.KILLED)
        check("kill_reaps", not killer.is_alive())

        # 3. hard_kill_pid on an already-dead pid reports success.
        check("kill_dead_pid_ok", hard_kill_pid(999999999, timeout=1.0) is True)

        # 4. restart preserves workdir state.
        state_file = Path(tmp) / "n3" / "data.txt"
        rst = NodeProcess("n3", [sys.executable, "-c", _SLEEP_PROG], f"{tmp}/n3")
        rst.spawn()
        state_file.write_text("recovery-marker", encoding="utf-8")
        old_pid = rst.pid
        new_pid = rst.restart(graceful=False, stop_timeout=15.0)
        check("restart_new_proc", new_pid != old_pid and rst.is_alive())
        check("restart_count", rst.restarts == 1)
        check(
            "restart_preserves_state",
            state_file.read_text(encoding="utf-8") == "recovery-marker",
        )
        check("restart_preserves_log", Path(rst.log_path).exists())
        rst.graceful_stop(timeout=10.0)

        # 5. marker watcher fires when the marker is logged.
        fired: list[tuple[str, str, str]] = []
        marker_node = NodeProcess(
            "n4",
            [
                sys.executable,
                "-c",
                "import sys, time; print('CRITICAL_WRITE_POINT', flush=True); "
                "time.sleep(60)",
            ],
            f"{tmp}/n4",
        )
        marker_node.spawn()
        watcher = MarkerWatcher(
            "n4",
            marker_node.log_path,
            CRITICAL_WRITE_MARKER,
            lambda nid, hit, line: fired.append((nid, hit, line)),
            poll_interval=0.02,
        )
        watcher.start()
        deadline = time.monotonic() + 15.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)
        watcher.stop()
        check("marker_fires", len(fired) == 1 and fired[0][1] == CRITICAL_WRITE_MARKER)
        marker_node.hard_kill(timeout=15.0)

        # 6. manager: spawn_all / watch_marker(kill) / shutdown.
        with SupervisorManager(f"{tmp}/mgr") as mgr:
            mgr.spawn_node("a", [sys.executable, "-c", _SLEEP_PROG])
            mgr.spawn_node(
                "b",
                [
                    sys.executable,
                    "-c",
                    "import time; print('CRITICAL_WRITE_POINT', flush=True); "
                    "time.sleep(60)",
                ],
            )
            pids = mgr.spawn_all()
            check("manager_spawn_all", set(pids) == {"a", "b"})
            mgr.watch_marker("b", poll_interval=0.02)
            deadline = time.monotonic() + 20.0
            killed = False
            while time.monotonic() < deadline:
                st = mgr.statuses()["b"]
                if st.state == NodeState.KILLED and not mgr.get("b").is_alive():
                    killed = True
                    break
                time.sleep(0.05)
            check("manager_marker_kill", killed)
            check("manager_statuses", set(mgr.statuses()) == {"a", "b"})
            stops = mgr.shutdown()
            check("manager_shutdown", all(stops.values()))

    return counts[0], counts[1]


def main() -> int:
    passed, failed = self_test()
    print(f"supervisor self-test: {passed} passed, {failed} failed", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
