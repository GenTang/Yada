"""SHA-bound transactional unified-diff editing."""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any

from yada.exceptions import ToolError
from yada.tools.base import ToolContext


def apply_patch(
    context: ToolContext,
    patch: str,
    expected_files: list[dict[str, str]],
) -> dict[str, Any]:
    """Apply a unified diff only when every target matches its declared version.

    Args:
        context: Shared workspace and verification state.
        patch: Git-style unified diff with one ``diff --git`` header per target.
        expected_files: Exact target set as ``path``/``sha256`` objects. New files
            use the literal digest ``NEW``.

    Returns:
        New workspace revision and post-apply hashes for all touched files.

    Raises:
        ToolError: If the diff is unsafe, stale, malformed, or cannot be applied.
    """

    if not isinstance(patch, str) or not patch.strip():
        raise ToolError("patch must be a non-empty unified diff")
    if len(patch) > 250_000:
        raise ToolError("patch exceeds the 250 KB limit")
    touched = _parse_patch_paths(context, patch)
    expected = _normalize_expected_files(context, expected_files)
    if set(expected) != set(touched):
        raise ToolError(
            "expected_files must exactly match patch paths; "
            f"patch={sorted(touched)}, expected={sorted(expected)}"
        )

    # The exact-set comparison above prevents a model from smuggling an unread
    # target into a multi-file patch. This loop then provides optimistic locking
    # for each existing file using the hash returned by read_file.
    for relative_path in sorted(touched):
        file_path = context.workspace.resolve(relative_path, allow_missing=True)
        declared = expected[relative_path]
        if file_path.exists():
            if not file_path.is_file():
                raise ToolError(f"patch target is not a regular file: {relative_path}")
            actual = context.workspace.sha256(file_path)
            if declared != actual:
                raise ToolError(
                    f"stale file hash for {relative_path}: expected {declared}, current {actual}"
                )
        elif declared != "NEW":
            raise ToolError(f"new file {relative_path} must use sha256 value NEW")

    # Validate the full transaction before mutating any path. ``git apply`` then
    # applies the same bytes, avoiding a custom diff parser with different rules.
    _git_apply(context, patch, check_only=True)
    _git_apply(context, patch, check_only=False)
    context.state.revision += 1
    context.state.patch_count += 1
    context.state.touched_files.update(touched)
    context.state.verified_revision = -1

    changed: list[dict[str, str]] = []
    for relative_path in sorted(touched):
        file_path = context.workspace.resolve(relative_path, allow_missing=True)
        changed.append(
            {
                "path": relative_path,
                "sha256": (
                    context.workspace.sha256(file_path)
                    if file_path.exists()
                    else "DELETED"
                ),
            }
        )
    return {
        "revision": context.state.revision,
        "changed_files": changed,
        "message": "patch applied; run a relevant test before finish",
    }


def _parse_patch_paths(context: ToolContext, patch: str) -> set[str]:
    forbidden_markers = (
        "GIT binary patch",
        "Binary files ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "old mode ",
        "new mode ",
        "new file mode 120000",
    )
    touched: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(forbidden_markers):
            raise ToolError(
                "binary, rename, copy, mode, and symlink patches are not supported"
            )
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise ToolError(f"invalid diff header: {line}") from exc
        if (
            len(parts) != 4
            or not parts[2].startswith("a/")
            or not parts[3].startswith("b/")
        ):
            raise ToolError(f"unsupported diff header: {line}")
        old_path = parts[2][2:]
        new_path = parts[3][2:]
        if old_path != new_path:
            raise ToolError("renames are not supported in the minimal patch tool")
        normalized = context.workspace.display(
            context.workspace.resolve(new_path, allow_missing=True)
        )
        touched.add(normalized)
    if not touched:
        raise ToolError("patch must contain at least one 'diff --git a/... b/...' header")
    return touched


def _normalize_expected_files(
    context: ToolContext, expected_files: list[dict[str, str]]
) -> dict[str, str]:
    if not isinstance(expected_files, list) or not expected_files:
        raise ToolError("expected_files must be a non-empty array")
    normalized: dict[str, str] = {}
    for item in expected_files:
        if not isinstance(item, dict):
            raise ToolError("each expected_files entry must be an object")
        path = item.get("path")
        sha256 = item.get("sha256")
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise ToolError("expected_files entries require string path and sha256")
        relative = context.workspace.display(
            context.workspace.resolve(path, allow_missing=True)
        )
        if relative in normalized:
            raise ToolError(f"duplicate expected_files path: {relative}")
        if sha256 != "NEW" and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ToolError(f"invalid sha256 for {relative}")
        normalized[relative] = sha256
    return normalized


def _git_apply(context: ToolContext, patch: str, *, check_only: bool) -> None:
    argv = ["git", "apply", "--whitespace=nowarn"]
    if check_only:
        argv.append("--check")
    argv.append("-")
    result = subprocess.run(
        argv,
        cwd=context.workspace.root,
        input=patch,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        phase = "check" if check_only else "apply"
        error = (result.stderr or result.stdout).strip()
        raise ToolError(f"git apply {phase} failed: {error[:2000]}")
