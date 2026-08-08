"""Disposable Git workspaces for Host-enforced Red-Green verification."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from yada.environments.workspace import Workspace
from yada.exceptions import ToolError


class VerificationWorkspaces:
    """Own the canonical, Red staging, and per-command Fix worktrees."""

    def __init__(self, primary: Workspace) -> None:
        self.primary = primary
        self._red_temp_root: Path | None = None
        self._red_root: Path | None = None
        self._runtime_snapshot_temp_root: Path | None = None
        self._runtime_snapshot_root: Path | None = None

    def start_red(self, baseline: str) -> Workspace:
        """Create a persistent Red staging worktree at the clean baseline."""

        if self._red_root is not None:
            raise ToolError(
                "Red workspace has already been created",
                error_code="red_workspace_failed",
            )
        # Freeze the complete prepared runtime view once. Red and every later Fix
        # command are materialized from this same immutable snapshot, so generated
        # files, empty directories, symlinks, and permissions match the canonical
        # workspace as it existed at strategy-selection time.
        self._create_runtime_snapshot()
        try:
            temp_root, red_root = self._create_worktree("yada-red-", baseline)
        except ToolError:
            self._close_runtime_snapshot()
            raise
        self._red_temp_root = temp_root
        self._red_root = red_root
        return Workspace(red_root)

    def materialize_test(self, patch: str) -> None:
        """Apply a frozen test patch to the unchanged canonical workspace."""

        status = _run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude).yada/**",
            ],
            self.primary.root,
            timeout=30,
        )
        if status.returncode or status.stdout.strip():
            raise ToolError(
                "canonical workspace changed while Red was running",
                error_code="baseline_changed",
            )
        self._apply_patch(self.primary.root, patch, check_first=True)
        self.close_red()

    @contextmanager
    def fix_command(self, baseline: str) -> Iterator[tuple[Workspace, str]]:
        """Yield an isolated latest-candidate workspace and its source patch."""

        candidate_patch = self.collect_patch(self.primary.root)
        temp_root, command_root = self._create_worktree("yada-fix-command-", baseline)
        try:
            if candidate_patch:
                self._apply_patch(command_root, candidate_patch)
            yield Workspace(command_root), candidate_patch
        finally:
            self._remove_worktree(command_root, temp_root)

    @staticmethod
    def collect_patch(workspace: Path) -> str:
        """Collect tracked and untracked candidate changes, excluding Yada state."""

        tracked = _run(["git", "diff", "--binary", "HEAD", "--", "."], workspace)
        if tracked.returncode:
            raise ToolError(
                "could not collect candidate patch",
                error_code="verification_workspace_failed",
            )
        untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            workspace,
        )
        if untracked.returncode:
            raise ToolError(
                "could not enumerate candidate files",
                error_code="verification_workspace_failed",
            )
        patches = [tracked.stdout]
        for path in filter(None, untracked.stdout.split("\0")):
            if path == ".yada" or path.startswith(".yada/"):
                continue
            candidate = workspace / path
            if not candidate.is_file():
                continue
            diff = _run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--binary",
                    "--",
                    "/dev/null",
                    path,
                ],
                workspace,
            )
            if diff.returncode not in {0, 1}:
                raise ToolError(
                    f"could not collect candidate file: {path}",
                    error_code="verification_workspace_failed",
                )
            patches.append(diff.stdout)
        return "".join(patches)

    def close_red(self) -> None:
        """Discard the Red staging worktree if it is still active."""

        if self._red_root is not None and self._red_temp_root is not None:
            self._remove_worktree(self._red_root, self._red_temp_root)
        self._red_root = None
        self._red_temp_root = None

    def close(self) -> None:
        self.close_red()
        self._close_runtime_snapshot()

    def _create_worktree(self, prefix: str, baseline: str) -> tuple[Path, Path]:
        temp_root = Path(tempfile.mkdtemp(prefix=prefix))
        worktree = temp_root / "workspace"
        result = _run(
            ["git", "worktree", "add", "--detach", str(worktree), baseline],
            self.primary.root,
        )
        if result.returncode:
            temp_root.rmdir()
            raise ToolError(
                "could not create isolated verification workspace",
                error_code="verification_workspace_failed",
                details={"stderr": result.stderr[-1000:]},
            )
        try:
            self._materialize_runtime_snapshot(worktree)
        except ToolError:
            self._remove_worktree(worktree, temp_root)
            raise
        return temp_root, worktree

    def _create_runtime_snapshot(self) -> None:
        """Copy the canonical runtime tree once, excluding Host-owned state."""

        if self._runtime_snapshot_root is not None:
            raise ToolError(
                "runtime snapshot has already been created",
                error_code="verification_workspace_failed",
            )
        temp_root = Path(tempfile.mkdtemp(prefix="yada-runtime-snapshot-"))
        snapshot = temp_root / "workspace"
        try:
            shutil.copytree(
                self.primary.root,
                snapshot,
                symlinks=True,
                ignore=_ignore_host_state,
            )
        except OSError as exc:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise ToolError(
                "could not snapshot the prepared runtime workspace",
                error_code="verification_workspace_failed",
                details={"error": str(exc)},
            ) from exc
        self._runtime_snapshot_temp_root = temp_root
        self._runtime_snapshot_root = snapshot

    def _materialize_runtime_snapshot(self, worktree: Path) -> None:
        """Replace Git-populated files with the frozen prepared runtime tree."""

        snapshot = self._runtime_snapshot_root
        if snapshot is None:
            raise ToolError(
                "runtime snapshot is unavailable",
                error_code="verification_workspace_failed",
            )
        try:
            for child in worktree.iterdir():
                if child.name == ".git":
                    continue
                _remove_path(child)
            shutil.copytree(snapshot, worktree, symlinks=True, dirs_exist_ok=True)
        except OSError as exc:
            raise ToolError(
                "could not materialize the prepared runtime snapshot",
                error_code="verification_workspace_failed",
                details={"error": str(exc)},
            ) from exc

    def _close_runtime_snapshot(self) -> None:
        if self._runtime_snapshot_temp_root is not None:
            shutil.rmtree(self._runtime_snapshot_temp_root, ignore_errors=True)
        self._runtime_snapshot_temp_root = None
        self._runtime_snapshot_root = None

    def _remove_worktree(self, worktree: Path, temp_root: Path) -> None:
        _run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            self.primary.root,
        )
        if temp_root.exists():
            try:
                temp_root.rmdir()
            except OSError:
                pass

    @staticmethod
    def _apply_patch(workspace: Path, patch: str, *, check_first: bool = False) -> None:
        phases = (True, False) if check_first else (False,)
        for check_only in phases:
            argv = ["git", "apply", "--whitespace=nowarn", "--recount"]
            if check_only:
                argv.append("--check")
            argv.append("-")
            result = _run(argv, workspace, input_text=patch, timeout=30)
            if result.returncode:
                raise ToolError(
                    "could not apply candidate patch in verification workspace",
                    error_code="verification_workspace_failed",
                    details={"stderr": result.stderr[-1000:]},
                )


def _run(
    argv: list[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def _ignore_host_state(_: str, names: list[str]) -> set[str]:
    """Exclude repository and Yada control state from runtime snapshots."""

    return {name for name in names if name in {".git", ".yada"}}


def _remove_path(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


__all__ = ["VerificationWorkspaces"]
