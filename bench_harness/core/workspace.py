"""Workspace sandbox creation, Git baseline tracking and file isolation."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from benchmark_v3.bench_harness.core.runner import (
    bind_to_kill_on_close_job,
    close_job_handle,
    kill_process_tree,
)


class WorkspaceManager:
    """Per-task isolated workspace with Git baseline tracking.

    Parameters
    ----------
    base_dir:
        Directory under which the task workspace is created.
    task_id:
        Task identifier; becomes the workspace directory name (sanitized).
    baseline_ref:
        Git ref recorded as the evaluation baseline (default ``"HEAD"``).
    keep_on_cleanup:
        When True, :meth:`teardown` leaves files on disk (debugging).
    """

    def __init__(
        self,
        base_dir: str | Path,
        task_id: str,
        baseline_ref: str = "HEAD",
        keep_on_cleanup: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir)
        safe_task_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in task_id)
        self.task_id = task_id
        self.workspace_dir = self.base_dir / safe_task_id
        self.baseline_ref = baseline_ref
        self.keep_on_cleanup = keep_on_cleanup
        self.baseline_commit: str | None = None

    # -- lifecycle ------------------------------------------------------

    def setup(self, create_git: bool = False) -> Path:
        """Create the sandbox directory.

        Git baseline is *not* created here: fixtures are written by
        ``prepare_task`` afterwards. Call :meth:`ensure_git_baseline`
        once the workspace tree is complete. ``create_git=True`` is
        kept for callers that already have files in place.
        """
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if create_git:
            self.ensure_git_baseline()
        else:
            self.record_baseline()
        return self.workspace_dir

    def ensure_git_baseline(self) -> str | None:
        """Init the workspace repo and commit the current tree as baseline.

        Uses ``git -c user.*`` only (never writes the user gitconfig).
        Fail-open: missing git or commit errors leave ``baseline_commit`` unset
        so evaluation still proceeds.
        """
        try:
            if not (self.workspace_dir / ".git").exists():
                self._git("init")
            self._git("add", "-A")
            self._git(
                "-c", "user.email=bench-harness@localhost",
                "-c", "user.name=bench-harness",
                "-c", "commit.gpgsign=false",
                "commit", "-m", "baseline", "--allow-empty", "--no-gpg-sign",
            )
        except Exception:
            self.baseline_commit = None
            return None
        return self.record_baseline()

    def record_baseline(self) -> str | None:
        """Record the current baseline commit SHA (None when not a repo)."""
        result = self._git("rev-parse", self.baseline_ref, check=False)
        sha = result.strip() if result is not None else ""
        self.baseline_commit = sha or None
        return self.baseline_commit

    def teardown(self) -> None:
        """Remove the sandbox unless ``keep_on_cleanup`` is set."""
        if self.keep_on_cleanup:
            return
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir, ignore_errors=True)

    # -- diff extraction -------------------------------------------------

    def get_unstaged_diff(self) -> str:
        """Return ``git diff`` (unstaged working-tree changes)."""
        return self._git("diff", "--", ".", check=False) or ""

    def get_diff_vs_baseline(self, pathspec: str = ".") -> str:
        """Return the diff between the recorded baseline and the worktree."""
        if not self.baseline_commit:
            return self.get_unstaged_diff()
        return self._git("diff", self.baseline_commit, "--", pathspec, check=False) or ""

    def get_status_short(self) -> str:
        """Return ``git status --short`` (empty when not a repo)."""
        return self._git("status", "--short", check=False) or ""

    # -- isolation ---------------------------------------------------------

    def reset_file(self, relpath: str) -> None:
        """Restore a single file to the baseline state."""
        target = self.baseline_ref if not self.baseline_commit else self.baseline_commit
        self._git("checkout", target, "--", relpath)

    def reset_all(self) -> None:
        """Restore the whole worktree to the baseline state."""
        target = self.baseline_ref if not self.baseline_commit else self.baseline_commit
        self._git("checkout", target, "--", ".")

    def resolve(self, relpath: str) -> Path:
        """Resolve a workspace-relative path, guarding against escape."""
        candidate = (self.workspace_dir / relpath).resolve()
        root = self.workspace_dir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes workspace: {relpath!r}")
        return candidate

    # -- internals ---------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> str | None:
        # Windows: bind the short-lived git child to a kill-on-close Job
        # Object so no git helper/credential process survives the harness.
        # Fail-open: any job/Win32 failure must not affect the git call.
        job = 0
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                ["git", *args],
                cwd=self.workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **( {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                    if sys.platform == "win32" else {} ),
            )
        except OSError as exc:
            if check:
                raise RuntimeError(f"git {' '.join(args)} failed to start: {exc}") from exc
            return None
        try:
            # Bind only after a successful spawn; a failed bind is a no-op.
            job = bind_to_kill_on_close_job(proc.pid)
            stdout, stderr = proc.communicate(timeout=30)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            if check:
                raise RuntimeError(f"git {' '.join(args)} timed out after 30s")
            return None
        except (OSError, subprocess.SubprocessError):
            kill_process_tree(proc.pid)
            if check:
                raise
            return None
        finally:
            # Closing the job handle reaps any leaked git helper tree.
            close_job_handle(job)
        if returncode != 0:
            if check:
                raise RuntimeError(
                    f"git {' '.join(args)} failed: {(stderr or '').strip()}"
                )
            return None
        return stdout
