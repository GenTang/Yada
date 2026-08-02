"""Composition root and dispatcher for Yada's tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from yada.environments import CommandApprover, Workspace
from yada.exceptions import ToolError
from yada.tools.base import ToolContext, ToolExecution
from yada.tools.command import run_command
from yada.tools.finish import final_state, finish
from yada.tools.patch import apply_patch
from yada.tools.read import read_file
from yada.tools.schemas import TOOL_SCHEMAS
from yada.tools.search import search_code

ToolHandler = Callable[..., dict[str, Any]]


class ToolRunner:
    """Own shared tool state and route model tool calls to small handlers."""

    def __init__(
        self,
        workspace: Path,
        *,
        command_policy: str = "ask",
        command_timeout_seconds: int = 120,
        max_output_chars: int = 12_000,
        command_environment: dict[str, str] | None = None,
        approver: CommandApprover | None = None,
    ) -> None:
        self.context = ToolContext(
            workspace=Workspace(workspace),
            approver=approver or CommandApprover(command_policy),
            command_timeout_seconds=command_timeout_seconds,
            max_output_chars=max_output_chars,
            command_environment=dict(command_environment or {}),
        )
        self._handlers: dict[str, ToolHandler] = {
            "search_code": search_code,
            "read_file": read_file,
            "apply_patch": apply_patch,
            "run_command": run_command,
        }

    @property
    def workspace(self) -> Workspace:
        """Expose the workspace boundary used by all registered handlers."""

        return self.context.workspace

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """Return the stable tool schemas sent with every model request."""

        return TOOL_SCHEMAS

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        """Dispatch one tool call and normalize expected validation failures.

        Args:
            name: Tool name from the model response.
            arguments: Decoded JSON object for the selected handler.

        Returns:
            A model-safe result. Expected tool and argument errors become
            ``{"ok": false, ...}`` observations instead of escaping the loop.
        """

        try:
            if name == "finish":
                return finish(self.context, **arguments)
            handler = self._handlers.get(name)
            if handler is None:
                raise ToolError(f"unknown tool: {name}")
            data = handler(self.context, **arguments)
            return ToolExecution({"ok": True, **data})
        except (ToolError, TypeError, ValueError) as exc:
            return ToolExecution({"ok": False, "error": str(exc)})

    def final_state(self) -> dict[str, Any]:
        """Collect the bounded Git status and diff used in the final result."""

        return final_state(self.context)
