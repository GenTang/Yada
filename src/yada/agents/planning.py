"""Side-effect-free planning policy for the default agent loop.

The planner in this module is deliberately small. It does not make a second model
request; instead, it turns one model response into a validated description of what
the executor may do next. Keeping this policy separate makes it possible to add a
dedicated planning model later without coupling it to workspace mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yada.agents.prompts import SYSTEM_PROMPT, task_prompt


@dataclass(frozen=True)
class StepPlan:
    """The planner's side-effect-free decision for one assistant turn.

    Attributes:
        tool_calls: Tool calls that may be handed to the executor.
        consecutive_text_turns: Number of consecutive responses without tools.
        display_text: Assistant text that should be shown to the user.
        reminder: Protocol reminder to append to the conversation, if needed.
        rejection_error: Batch-level protocol error that rejects every tool call.
    """

    tool_calls: tuple[dict[str, Any], ...]
    consecutive_text_turns: int
    display_text: str = ""
    reminder: str | None = None
    rejection_error: str | None = None


class Planner:
    """Translate model messages into validated plans without performing I/O.

    This boundary owns conversation policy: initial prompts, recovery from
    text-only responses, and constraints that span a batch of tool calls. It does
    not know how any tool is implemented and cannot modify the workspace.
    """

    def initial_messages(self, task: str) -> list[dict[str, Any]]:
        """Build the stable message prefix for a user task.

        Args:
            task: Non-empty coding task supplied by the user.

        Returns:
            A new system/user message list ready for the first model request.

        Raises:
            ValueError: If ``task`` contains only whitespace.
        """

        if not task.strip():
            raise ValueError("task must not be empty")
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt(task)},
        ]

    def plan(
        self,
        assistant_message: dict[str, Any],
        *,
        consecutive_text_turns: int,
    ) -> StepPlan:
        """Plan the next action from a single assistant message.

        Args:
            assistant_message: OpenAI-compatible assistant response payload.
            consecutive_text_turns: Text-only response count before this turn.

        Returns:
            A :class:`StepPlan` containing either executable calls or a recovery
            reminder. A mixed ``finish`` batch is preserved for traceability but
            marked with ``rejection_error`` so the executor cannot run it.
        """

        tool_calls = tuple(assistant_message.get("tool_calls") or ())
        if not tool_calls:
            text_turns = consecutive_text_turns + 1
            reminder = (
                "Continue working with tools. You must call finish after a patch and a "
                "successful test/build; a text-only response does not complete the "
                "task."
            )
            if text_turns >= 3:
                reminder += (
                    " This is your final reminder to use the required tool protocol."
                )
            return StepPlan(
                tool_calls=(),
                consecutive_text_turns=text_turns,
                display_text=str(assistant_message.get("content") or "").strip(),
                reminder=reminder,
            )

        rejection_error = None
        if len(tool_calls) > 1 and any(
            _tool_name(call) == "finish" for call in tool_calls
        ):
            # A concurrent finish could report success while sibling calls are still
            # mutating or verifying the repository, so reject the entire batch.
            rejection_error = "finish must be the only tool call in its assistant turn"
        return StepPlan(
            tool_calls=tool_calls,
            consecutive_text_turns=0,
            rejection_error=rejection_error,
        )


def _tool_name(call: dict[str, Any]) -> str:
    name = (call.get("function") or {}).get("name")
    return str(name) if name else "<missing>"
