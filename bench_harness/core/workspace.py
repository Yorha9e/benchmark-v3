"""Workspace sandbox creation, Git baseline tracking and file isolation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


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
        """Create the sandbox directory and record the Git baseline."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if create_git and not (self.workspace_dir / ".git").exists():
            self._git("init")
            self._git("add", "-A")
            self._git("commit", "-m", "baseline", "--allow-empty")
        self.record_baseline()
        return self.workspace_dir

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
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            if check:
                raise
            return None
        if completed.returncode != 0:
            if check:
                raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
            return None
        return completed.stdout
