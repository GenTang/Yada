"""Collect complete Git patches without coupling to a specific agent."""

from __future__ import annotations

import subprocess
from pathlib import Path


class PatchCollectionError(RuntimeError):
    """Raised when a candidate workspace cannot produce a trustworthy patch."""


def collect_git_patch(workspace: Path) -> str:
    """Return tracked and untracked changes relative to ``HEAD``."""

    root = workspace.expanduser().resolve()
    if not (root / ".git").exists():
        raise PatchCollectionError(f"workspace is not a Git repository: {root}")

    tracked = _run(
        ["git", "diff", "--binary", "HEAD", "--", "."],
        root,
    )
    if tracked.returncode:
        raise PatchCollectionError(tracked.stderr.strip() or "git diff failed")

    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        root,
    )
    if untracked.returncode:
        raise PatchCollectionError(
            untracked.stderr.strip() or "git ls-files failed"
        )

    patches = [tracked.stdout]
    for relative in filter(None, untracked.stdout.split("\0")):
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PatchCollectionError(
                f"untracked path escapes workspace: {relative}"
            ) from exc
        if not candidate.is_file():
            continue
        diff = _run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative],
            root,
        )
        if diff.returncode not in {0, 1}:
            raise PatchCollectionError(
                diff.stderr.strip() or f"cannot diff untracked file: {relative}"
            )
        patches.append(diff.stdout)
    return "".join(patches)


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
