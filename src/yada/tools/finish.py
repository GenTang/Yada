"""Verification gate and final repository state collection."""

from __future__ import annotations

import subprocess
from typing import Any

from yada.exceptions import ToolError
from yada.tools.base import ToolContext, ToolExecution
from yada.utils.text import truncate_text


def finish_task(context: ToolContext, summary: str) -> ToolExecution:
    """Complete a run only after the latest revision passes verification.

    Args:
        context: Shared tool state and workspace.
        summary: Concise model-authored description of the completed change.

    Returns:
        Terminal execution containing verification history and final Git state.

    Raises:
        ToolError: If no patch exists, tests are stale, or the diff has errors.
    """

    if not isinstance(summary, str) or not summary.strip():
        raise ToolError("summary must be a non-empty string")
    if context.state.patch_count == 0:
        raise ToolError("finish_task rejected: no patch has been applied")
    if context.state.verified_revision != context.state.revision:
        raise ToolError(
            "finish_task rejected: run a successful test or build after the latest patch"
        )
    diff_check = _git_diff_check(context)
    if diff_check:
        raise ToolError(f"finish_task rejected by git diff --check: {diff_check}")
    return ToolExecution(
        {
            "ok": True,
            "status": "finished",
            "summary": summary.strip(),
            "revision": context.state.revision,
            "successful_verifications": context.state.successful_verifications,
            **final_state(context),
        },
        finished=True,
    )


def _git_diff_check(context: ToolContext) -> str:
    if not (context.workspace.root / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "diff", "--check", "--"],
        cwd=context.workspace.root,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return "" if result.returncode == 0 else (result.stdout + result.stderr).strip()


def final_state(context: ToolContext) -> dict[str, Any]:
    """Collect bounded Git status and diffs, including newly created files.

    Args:
        context: Workspace and touched-file state for the current run.

    Returns:
        ``git_status``, ``diff_stat``, and ``diff`` fields, or ``None`` values
        outside a Git repository.
    """

    if not (context.workspace.root / ".git").exists():
        return {"git_status": None, "diff_stat": None, "diff": None}
    commands = {
        "git_status": [
            "git",
            "status",
            "--short",
            "--",
            ".",
            ":(exclude).yada/**",
        ],
        "diff_stat": ["git", "diff", "--stat", "--"],
        "diff": ["git", "diff", "--", "."],
    }
    output: dict[str, Any] = {}
    for key, argv in commands.items():
        result = subprocess.run(
            argv,
            cwd=context.workspace.root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        text, truncated = truncate_text(result.stdout + result.stderr, 30_000)
        output[key] = text
        if truncated:
            output[f"{key}_truncated"] = True

    # Plain ``git diff`` omits untracked files. Reconstruct only files touched by
    # this agent rather than dumping every unrelated untracked artifact.
    untracked_diffs: list[str] = []
    for relative_path in sorted(context.state.touched_files):
        file_path = context.workspace.resolve(relative_path, allow_missing=True)
        if not file_path.exists():
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative_path],
            cwd=context.workspace.root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if tracked.returncode == 0:
            continue
        diff = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", relative_path],
            cwd=context.workspace.root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        untracked_diffs.append(diff.stdout)
    if untracked_diffs:
        combined = (output.get("diff") or "") + "".join(untracked_diffs)
        output["diff"], output["diff_truncated"] = truncate_text(combined, 30_000)
    return output
