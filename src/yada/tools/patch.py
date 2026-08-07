"""SHA-bound transactional unified-diff editing."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from yada.exceptions import ToolError
from yada.tools.base import ToolContext

_MAX_ERROR_CHARS = 2_000
_MAX_GIT_ERROR_CHARS = 1_000
_MAX_DETAIL_PATHS = 20
_MAX_PATH_CHARS = 300


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
        raise _patch_error(
            "patch must be a non-empty unified diff",
            "invalid_patch",
            recovery="Provide a non-empty Git-style unified diff.",
        )
    if len(patch) > 250_000:
        raise _patch_error(
            "patch exceeds the 250 KB limit",
            "invalid_patch",
            recovery="Split the change into smaller patches.",
        )
    touched = _parse_patch_paths(context, patch)
    context.workflow.authorize_mutation(touched)
    expected = _normalize_expected_files(context, expected_files)
    if set(expected) != set(touched):
        raise _patch_error(
            "expected_files must exactly match patch paths",
            "invalid_patch",
            details={
                "patch_paths": _bounded_paths(touched),
                "expected_paths": _bounded_paths(expected),
            },
            recovery="Declare exactly one current hash for every patch target.",
        )

    # The exact-set comparison above prevents a model from smuggling an unread
    # target into a multi-file patch. This loop then provides optimistic locking
    # for each existing file using the hash returned by read_file.
    for relative_path in sorted(touched):
        file_path = _resolve_patch_target(context, relative_path)
        declared = expected[relative_path]
        if file_path.exists():
            if not file_path.is_file():
                raise _patch_error(
                    f"patch target is not a regular file: {_clip_path(relative_path)}",
                    "unsupported_target",
                    paths=[relative_path],
                    recovery="Choose an existing regular file or a new file path.",
                )
            actual = context.workspace.sha256(file_path)
            if declared != actual:
                raise _patch_error(
                    f"stale file hash for {_clip_path(relative_path)}: "
                    f"expected {declared}, current {actual}",
                    "stale_hash",
                    paths=[relative_path],
                    details={
                        "expected_sha256": declared,
                        "current_sha256": actual,
                    },
                    recovery="Read the current file and regenerate the patch.",
                )
        elif declared != "NEW":
            raise _patch_error(
                f"new file {_clip_path(relative_path)} must use sha256 value NEW",
                "stale_hash",
                paths=[relative_path],
                details={
                    "expected_sha256": declared,
                    "current_sha256": "MISSING",
                },
                recovery="Read the workspace state and regenerate the patch.",
            )

    # Validate the full transaction before mutating any path. ``git apply`` then
    # applies the same bytes, avoiding a custom diff parser with different rules.
    _git_apply(context, patch, touched, check_only=True)
    _git_apply(context, patch, touched, check_only=False)
    context.state.revision += 1
    context.state.patch_count += 1
    context.state.touched_files.update(touched)
    context.state.verified_revision = -1
    context.workflow.record_mutation(touched, context.state.revision)

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
        "message": "patch applied; run a relevant test before finish_task",
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
            raise _patch_error(
                "binary, rename, copy, mode, and symlink patches are not supported",
                "unsupported_target",
                recovery="Use a regular text-file patch without mode or rename metadata.",
            )
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise _patch_error(
                f"invalid diff header: {_clip(line)}",
                "invalid_patch",
                recovery="Regenerate the unified diff header.",
            ) from exc
        if (
            len(parts) != 4
            or not parts[2].startswith("a/")
            or not parts[3].startswith("b/")
        ):
            raise _patch_error(
                f"unsupported diff header: {_clip(line)}",
                "invalid_patch",
                recovery="Use matching 'diff --git a/<path> b/<path>' targets.",
            )
        old_path = parts[2][2:]
        new_path = parts[3][2:]
        if old_path != new_path:
            raise _patch_error(
                "renames are not supported in the minimal patch tool",
                "unsupported_target",
                paths=[old_path, new_path],
                recovery="Represent supported content changes without a rename.",
            )
        normalized = context.workspace.display(_resolve_patch_target(context, new_path))
        touched.add(normalized)
    if not touched:
        raise _patch_error(
            "patch must contain at least one 'diff --git a/... b/...' header",
            "invalid_patch",
            recovery="Provide a Git-style unified diff with an explicit target header.",
        )
    return touched


def _normalize_expected_files(
    context: ToolContext, expected_files: list[dict[str, str]]
) -> dict[str, str]:
    if not isinstance(expected_files, list) or not expected_files:
        raise _patch_error(
            "expected_files must be a non-empty array",
            "invalid_patch",
            recovery="Declare the current hash for every patch target.",
        )
    normalized: dict[str, str] = {}
    for item in expected_files:
        if not isinstance(item, dict):
            raise _patch_error(
                "each expected_files entry must be an object",
                "invalid_patch",
            )
        path = item.get("path")
        sha256 = item.get("sha256")
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise _patch_error(
                "expected_files entries require string path and sha256",
                "invalid_patch",
            )
        relative = context.workspace.display(_resolve_patch_target(context, path))
        if relative in normalized:
            raise _patch_error(
                f"duplicate expected_files path: {_clip_path(relative)}",
                "invalid_patch",
                paths=[relative],
            )
        if sha256 != "NEW" and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise _patch_error(
                f"invalid sha256 for {_clip_path(relative)}",
                "invalid_patch",
                paths=[relative],
                recovery="Use the SHA-256 returned by read_file or NEW for a new file.",
            )
        normalized[relative] = sha256
    return normalized


def _git_apply(
    context: ToolContext,
    patch: str,
    touched: set[str],
    *,
    check_only: bool,
) -> None:
    argv = ["git", "apply", "--whitespace=nowarn", "--recount"]
    if check_only:
        argv.append("--check")
    argv.append("-")
    try:
        result = subprocess.run(
            argv,
            cwd=context.workspace.root,
            input=patch,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        phase = "check" if check_only else "apply"
        raise _patch_error(
            f"git apply {phase} could not run: {_clip(str(exc))}",
            "apply_failed",
            paths=touched,
            details={"phase": phase},
            recovery="Inspect the local Git installation and retry.",
        ) from exc
    if result.returncode != 0:
        phase = "check" if check_only else "apply"
        error = (result.stderr or result.stdout).strip()
        error_code = _classify_git_apply_error(error) if check_only else "apply_failed"
        recovery = (
            "Correct the unified diff and retry."
            if error_code == "invalid_patch"
            else "Read the current files and regenerate the patch."
            if error_code == "patch_context_mismatch"
            else "Inspect the Git diagnostic and retry from the current workspace state."
        )
        raise _patch_error(
            f"git apply {phase} failed: {_clip(error, _MAX_GIT_ERROR_CHARS)}",
            error_code,
            paths=touched,
            details={
                "phase": phase,
                "git_error": _clip(error, _MAX_GIT_ERROR_CHARS),
            },
            recovery=recovery,
        )


def _resolve_patch_target(context: ToolContext, user_path: str) -> Path:
    """Resolve a patch target and reject paths that traverse symlinks."""

    try:
        resolved = context.workspace.resolve(user_path, allow_missing=True)
    except ToolError as exc:
        raise _patch_error(
            str(exc),
            "unsupported_target",
            paths=[user_path] if isinstance(user_path, str) else [],
            recovery="Choose a non-protected path inside the workspace.",
        ) from exc

    raw = Path(user_path).expanduser()
    candidate = raw if raw.is_absolute() else context.workspace.root / raw
    try:
        lexical = candidate.absolute().relative_to(context.workspace.root)
    except ValueError as exc:
        raise _patch_error(
            f"path escapes workspace: {_clip_path(user_path)}",
            "unsupported_target",
            paths=[user_path],
            recovery="Choose a non-protected path inside the workspace.",
        ) from exc

    current = context.workspace.root
    for part in lexical.parts:
        current /= part
        if current.is_symlink():
            raise _patch_error(
                f"patch target traverses a symlink: {_clip_path(user_path)}",
                "unsupported_target",
                paths=[user_path],
                recovery="Patch the regular file directly instead of through a symlink.",
            )
    return resolved


def _classify_git_apply_error(error: str) -> str:
    lowered = error.lower()
    invalid_markers = (
        "corrupt patch",
        "unrecognized input",
        "no valid patches",
        "patch fragment without header",
        "git diff header lacks filename information",
        "invalid patch",
    )
    if any(marker in lowered for marker in invalid_markers):
        return "invalid_patch"
    return "patch_context_mismatch"


def _patch_error(
    message: str,
    error_code: str,
    *,
    paths: Any = (),
    details: dict[str, Any] | None = None,
    recovery: str | None = None,
) -> ToolError:
    payload = dict(details or {})
    bounded_paths = _bounded_paths(paths)
    if bounded_paths:
        payload["paths"] = bounded_paths
    if recovery is not None:
        payload["recovery"] = _clip(recovery)
    return ToolError(
        _clip(message, _MAX_ERROR_CHARS),
        error_code=error_code,
        details=payload or None,
    )


def _bounded_paths(paths: Any) -> list[str]:
    if isinstance(paths, dict):
        values = paths.keys()
    elif isinstance(paths, str):
        values = [paths]
    else:
        values = paths
    return [_clip_path(str(path)) for path in sorted(values)[:_MAX_DETAIL_PATHS]]


def _clip_path(path: str) -> str:
    return _clip(path, _MAX_PATH_CHARS)


def _clip(value: str, limit: int = _MAX_ERROR_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."
