"""Shared state and result types for tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from yada.environments.approval import CommandApprover
from yada.environments.commands import CommandExecutor, LocalCommandExecutor
from yada.environments.workspace import Workspace
from yada.exceptions import ToolError
from yada.verification import VerificationWorkflow, WorkflowEvent


@dataclass
class ToolState:
    """Mutable verification state shared by all tools in one agent run."""

    revision: int = 0
    verified_revision: int = -1
    patch_count: int = 0
    touched_files: set[str] = field(default_factory=set)
    successful_verifications: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolContext:
    """Workspace, policy, limits, and state passed to every tool handler."""

    workspace: Workspace
    workflow: VerificationWorkflow
    approver: CommandApprover
    command_timeout_seconds: int = 120
    max_output_chars: int = 12_000
    command_environment: dict[str, str] = field(default_factory=dict)
    command_executor: CommandExecutor = field(default_factory=LocalCommandExecutor)
    state: ToolState = field(default_factory=ToolState)


@dataclass(frozen=True)
class ToolExecution:
    """Structured tool result plus an explicit terminal-state marker."""

    data: dict[str, Any]
    finished: bool = False
    stop_reason: str | None = None
    events: tuple[WorkflowEvent, ...] = ()

    @property
    def content(self) -> str:
        """Serialize the observation exactly as it is sent back to the model."""

        return json.dumps(self.data, ensure_ascii=False)


__all__ = ["ToolContext", "ToolError", "ToolExecution", "ToolState"]
