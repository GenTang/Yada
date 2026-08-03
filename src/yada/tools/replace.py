"""Exact SHA-bound transactional text replacement."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yada.exceptions import ToolError
from yada.tools.base import ToolContext
from yada.tools.patch import _resolve_patch_target, apply_patch

_MAX_EDITS = 100
_MAX_EDIT_TEXT_CHARS = 100_000
_MAX_TOTAL_TEXT_CHARS = 200_000
_MAX_FILE_BYTES = 1_000_000
_MAX_GENERATED_PATCH_CHARS = 250_000
_MAX_MATCH_LINES = 20
_MAX_ERROR_CHARS = 2_000
_MAX_PATH_CHARS = 300
_MAX_PATH_INPUT_CHARS = 4_096
_EDIT_FIELDS = frozenset({"path", "sha256", "old_text", "new_text"})


@dataclass
class _FileReplacement:
    path: str
    sha256: str
    before: str
    after: str


def replace_text(
    context: ToolContext,
    edits: list[dict[str, str]],
) -> dict[str, Any]:
    """Apply ordered exact replacements as one checked patch transaction.

    Every path must identify an existing regular UTF-8 text file, and every
    replacement is evaluated against an in-memory version in declaration order.
    No workspace file is mutated until all edits validate.
    """

    if not isinstance(edits, list) or not edits:
        raise _replace_error(
            "edits must be a non-empty array",
            "invalid_edit",
            recovery="Provide at least one exact text replacement.",
        )
    if len(edits) > _MAX_EDITS:
        raise _replace_error(
            f"edits exceeds the {_MAX_EDITS}-item limit",
            "invalid_edit",
            recovery="Split the replacements into smaller transactions.",
        )

    files: dict[str, _FileReplacement] = {}
    total_text_chars = 0
    for index, item in enumerate(edits):
        path, sha256, old_text, new_text = _validate_edit(item, index)
        total_text_chars += len(old_text) + len(new_text)
        if total_text_chars > _MAX_TOTAL_TEXT_CHARS:
            raise _replace_error(
                f"combined old_text and new_text exceed the "
                f"{_MAX_TOTAL_TEXT_CHARS}-character limit",
                "invalid_edit",
                path=path,
                details={"edit_index": index},
                recovery="Split the replacements into smaller transactions.",
            )

        try:
            file_path = _resolve_patch_target(context, path)
        except ToolError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise _replace_error(
                f"could not resolve replace_text target {_clip_path(path)}: {exc}",
                "unsupported_target",
                path=path,
            ) from exc
        relative_path = context.workspace.display(file_path)
        state = files.get(relative_path)
        if state is None:
            state = _load_file(relative_path, file_path, sha256)
            files[relative_path] = state
        elif sha256 != state.sha256:
            raise _replace_error(
                f"all edits for {_clip_path(relative_path)} must use the same "
                "starting sha256",
                "invalid_edit",
                path=relative_path,
                details={"edit_index": index},
                recovery="Use the SHA-256 from the same read_file result.",
            )

        offsets = _find_match_offsets(state.after, old_text)
        if not offsets:
            raise _replace_error(
                f"old_text was not found in {_clip_path(relative_path)}",
                "no_match",
                path=relative_path,
                details={
                    "edit_index": index,
                    "current_sha256": state.sha256,
                },
                recovery="Read the current file and retry with exact source text.",
            )
        if len(offsets) > 1:
            match_lines = list(
                dict.fromkeys(
                    state.after.count("\n", 0, offset) + 1 for offset in offsets
                )
            )[:_MAX_MATCH_LINES]
            raise _replace_error(
                f"old_text is ambiguous in {_clip_path(relative_path)}",
                "ambiguous_match",
                path=relative_path,
                details={"edit_index": index, "match_lines": match_lines},
                recovery="Read a narrower range and provide a larger unique anchor.",
            )

        offset = offsets[0]
        state.after = (
            state.after[:offset] + new_text + state.after[offset + len(old_text) :]
        )

    for state in files.values():
        if state.after == state.before:
            raise _replace_error(
                f"edits produce no net change for {_clip_path(state.path)}",
                "invalid_edit",
                path=state.path,
                recovery="Remove cancelling edits or provide the intended replacement.",
            )

    patch = _build_patch(files)
    if len(patch) > _MAX_GENERATED_PATCH_CHARS:
        raise _replace_error(
            "generated patch exceeds the 250 KB transaction limit",
            "invalid_edit",
            details={"paths": _bounded_paths(files)},
            recovery="Split the replacements into smaller transactions.",
        )

    expected_files = [
        {"path": state.path, "sha256": state.sha256}
        for state in sorted(files.values(), key=lambda item: item.path)
    ]
    try:
        result = apply_patch(context, patch, expected_files)
    except ToolError as exc:
        if exc.error_code not in {"invalid_patch", "patch_context_mismatch"}:
            raise
        details = dict(exc.details or {})
        details["cause_error_code"] = exc.error_code
        raise _replace_error(
            "validated replacement transaction could not be applied",
            "apply_failed",
            details=details,
            recovery="Read the current files and retry from the latest workspace state.",
        ) from exc

    result["message"] = "text replaced; run a relevant test before finish"
    return result


def _validate_edit(
    item: Any,
    index: int,
) -> tuple[str, str, str, str]:
    if not isinstance(item, dict):
        raise _replace_error(
            "each edits entry must be an object",
            "invalid_edit",
            details={"edit_index": index},
        )
    if set(item) != _EDIT_FIELDS:
        raise _replace_error(
            "each edits entry must contain exactly path, sha256, old_text, and "
            "new_text",
            "invalid_edit",
            details={
                "edit_index": index,
                "missing_fields": sorted(_EDIT_FIELDS - set(item)),
                "unexpected_fields": sorted(
                    str(field) for field in set(item) - _EDIT_FIELDS
                )[:20],
            },
        )
    path = item.get("path")
    sha256 = item.get("sha256")
    old_text = item.get("old_text")
    new_text = item.get("new_text")
    if not all(isinstance(value, str) for value in (path, sha256, old_text, new_text)):
        raise _replace_error(
            "edits entries require string path, sha256, old_text, and new_text",
            "invalid_edit",
            details={"edit_index": index},
        )
    if not path.strip():
        raise _replace_error(
            "edit path must be a non-empty string",
            "invalid_edit",
            details={"edit_index": index},
        )
    if len(path) > _MAX_PATH_INPUT_CHARS:
        raise _replace_error(
            f"edit path exceeds the {_MAX_PATH_INPUT_CHARS}-character limit",
            "unsupported_target",
            path=path,
            details={"edit_index": index},
        )
    if any(character in path for character in ("\0", "\n", "\r", "\t")):
        raise _replace_error(
            "edit path contains an unsupported control character",
            "unsupported_target",
            path=path,
            details={"edit_index": index},
        )
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _replace_error(
            "edit path is not valid UTF-8",
            "unsupported_target",
            details={"edit_index": index},
        ) from exc
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise _replace_error(
            f"invalid sha256 for {_clip_path(path)}",
            "invalid_edit",
            path=path,
            details={"edit_index": index},
            recovery="Use the SHA-256 returned by read_file.",
        )
    if not old_text:
        raise _replace_error(
            "old_text must be non-empty",
            "invalid_edit",
            path=path,
            details={"edit_index": index},
        )
    if old_text == new_text:
        raise _replace_error(
            "old_text and new_text must differ",
            "invalid_edit",
            path=path,
            details={"edit_index": index},
        )
    if "\0" in old_text or "\0" in new_text:
        raise _replace_error(
            "old_text and new_text must not contain NUL bytes",
            "invalid_edit",
            path=path,
            details={"edit_index": index},
        )
    try:
        old_text.encode("utf-8")
        new_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _replace_error(
            "old_text and new_text must be valid UTF-8",
            "invalid_edit",
            path=path,
            details={"edit_index": index},
        ) from exc
    if max(len(old_text), len(new_text)) > _MAX_EDIT_TEXT_CHARS:
        raise _replace_error(
            f"old_text and new_text are limited to {_MAX_EDIT_TEXT_CHARS} "
            "characters per edit",
            "invalid_edit",
            path=path,
            details={"edit_index": index},
            recovery="Split the replacement into smaller edits.",
        )
    return path, sha256, old_text, new_text


def _load_file(
    path: str,
    file_path: Path,
    declared_sha256: str,
) -> _FileReplacement:
    if not file_path.exists():
        raise _replace_error(
            f"replace_text only supports existing files: {_clip_path(path)}",
            "unsupported_target",
            path=path,
            recovery="Use apply_patch to create a new file.",
        )
    if not file_path.is_file():
        raise _replace_error(
            f"replace_text target is not a regular file: {_clip_path(path)}",
            "unsupported_target",
            path=path,
        )
    try:
        with file_path.open("rb") as handle:
            content = handle.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise _replace_error(
            f"could not read replace_text target {_clip_path(path)}: {exc}",
            "unsupported_target",
            path=path,
        ) from exc
    if len(content) > _MAX_FILE_BYTES:
        raise _replace_error(
            f"replace_text target exceeds the {_MAX_FILE_BYTES}-byte limit",
            "unsupported_target",
            path=path,
            recovery="Use apply_patch for an unsuitable large structural edit.",
        )
    if b"\0" in content:
        raise _replace_error(
            f"replace_text target appears to be binary: {_clip_path(path)}",
            "unsupported_target",
            path=path,
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _replace_error(
            f"replace_text target is not valid UTF-8: {_clip_path(path)}",
            "unsupported_target",
            path=path,
        ) from exc

    actual_sha256 = hashlib.sha256(content).hexdigest()
    if declared_sha256 != actual_sha256:
        raise _replace_error(
            f"stale file hash for {_clip_path(path)}: expected {declared_sha256}, "
            f"current {actual_sha256}",
            "stale_hash",
            path=path,
            details={
                "expected_sha256": declared_sha256,
                "current_sha256": actual_sha256,
            },
            recovery="Read the current file and retry with its latest SHA-256.",
        )
    return _FileReplacement(path, declared_sha256, text, text)


def _find_match_offsets(text: str, old_text: str) -> list[int]:
    offsets: list[int] = []
    start = 0
    while len(offsets) <= _MAX_MATCH_LINES:
        offset = text.find(old_text, start)
        if offset < 0:
            break
        offsets.append(offset)
        start = offset + 1
    return offsets


def _build_patch(files: dict[str, _FileReplacement]) -> str:
    chunks: list[str] = []
    for state in sorted(files.values(), key=lambda item: item.path):
        old_path = _quote_diff_path(f"a/{state.path}")
        new_path = _quote_diff_path(f"b/{state.path}")
        chunks.append(f"diff --git {old_path} {new_path}\n")
        diff = difflib.unified_diff(
            _split_lf_lines(state.before),
            _split_lf_lines(state.after),
            fromfile=old_path,
            tofile=new_path,
            lineterm="\n",
        )
        for line in diff:
            chunks.append(line)
            if line[:1] in {" ", "+", "-"} and not line.endswith("\n"):
                chunks.append("\n\\ No newline at end of file\n")
    return "".join(chunks)


def _split_lf_lines(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [f"{part}\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _quote_diff_path(path: str) -> str:
    if all(ord(character) >= 33 and character not in {'"', "\\"} for character in path):
        return path
    return json.dumps(path, ensure_ascii=False)


def _replace_error(
    message: str,
    error_code: str,
    *,
    path: str | None = None,
    details: dict[str, Any] | None = None,
    recovery: str | None = None,
) -> ToolError:
    payload = dict(details or {})
    if path is not None:
        payload["paths"] = [_clip_path(path)]
    if recovery is not None:
        payload["recovery"] = _clip(recovery)
    return ToolError(
        _clip(message),
        error_code=error_code,
        details=payload or None,
    )


def _bounded_paths(paths: Any) -> list[str]:
    values = paths.keys() if isinstance(paths, dict) else paths
    return [_clip_path(str(path)) for path in sorted(values)[:20]]


def _clip_path(path: str) -> str:
    return _clip(path, _MAX_PATH_CHARS)


def _clip(value: str, limit: int = _MAX_ERROR_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."
