"""Shared state and result types for tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from yada.environments.approval import CommandApprover
from yada.environments.workspace import Workspace
from yada.exceptions import ToolError


@dataclass
class ToolState:
    revision: int = 0
    verified_revision: int = -1
    patch_count: int = 0
    touched_files: set[str] = field(default_factory=set)
    successful_verifications: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolContext:
    workspace: Workspace
    approver: CommandApprover
    command_timeout_seconds: int = 120
    max_output_chars: int = 12_000
    state: ToolState = field(default_factory=ToolState)


@dataclass(frozen=True)
class ToolExecution:
    data: dict[str, Any]
    finished: bool = False

    @property
    def content(self) -> str:
        return json.dumps(self.data, ensure_ascii=False)


__all__ = ["ToolContext", "ToolError", "ToolExecution", "ToolState"]

