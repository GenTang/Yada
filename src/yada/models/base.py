"""Model-neutral completion types used by the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Completion:
    message: dict[str, Any]
    usage: dict[str, Any]
    response_id: str | None = None
    model: str | None = None
    system_fingerprint: str | None = None
    finish_reason: str | None = None


class CompletionClient(Protocol):
    model: str

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion: ...

