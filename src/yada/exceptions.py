"""Yada-specific exceptions."""

from __future__ import annotations

from typing import Any


class ToolError(RuntimeError):
    """Raised when a tool request violates its contract or cannot be executed.

    ``error_code`` and ``details`` are optional so existing tools keep their
    original error observations. Tools with recovery-aware failures can attach
    a stable code and small, JSON-serializable evidence for the next agent turn.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details) if details is not None else None
