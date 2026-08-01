"""Workspace boundary and content hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from yada.exceptions import ToolError


PROTECTED_PARTS = {".git", ".yada"}


class Workspace:
    def __init__(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {resolved}")
        self.root = resolved

    def resolve(self, user_path: str, *, allow_missing: bool = False) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise ToolError("path must be a non-empty string")
        raw = Path(user_path).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"path escapes workspace: {user_path}") from exc
        if any(part in PROTECTED_PARTS for part in relative.parts):
            raise ToolError(f"access to protected path is denied: {user_path}")
        if not allow_missing and not resolved.exists():
            raise ToolError(f"path does not exist: {user_path}")
        return resolved

    def display(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return relative.as_posix() if relative.parts else "."

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
