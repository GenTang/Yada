"""Human approval policy for repository commands."""

from __future__ import annotations

import shlex
from typing import Callable


class CommandApprover:
    """Approval gate for commands. This is a guardrail, not an OS sandbox."""

    def __init__(
        self,
        mode: str = "ask",
        *,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        if mode not in {"ask", "allow", "deny"}:
            raise ValueError("command policy must be ask, allow, or deny")
        self.mode = mode
        self.input_fn = input_fn
        self.allow_rest = mode == "allow"

    def approve(self, argv: list[str], cwd: str) -> bool:
        if self.mode == "deny":
            return False
        if self.allow_rest:
            return True
        rendered = shlex.join(argv)
        answer = self.input_fn(
            f"\nYada wants to run in {cwd}:\n  {rendered}\nAllow? [y/N/a=allow rest] "
        ).strip().lower()
        if answer == "a":
            self.allow_rest = True
            return True
        return answer in {"y", "yes"}

