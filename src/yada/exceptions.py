"""Yada-specific exceptions."""


class ToolError(RuntimeError):
    """Raised when a tool request violates its contract or cannot be executed."""

