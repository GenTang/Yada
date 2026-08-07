"""Tool-call execution boundary for Yada agents."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from yada.tools import ToolRunner
from yada.tools.base import ToolExecution
from yada.traces import TraceWriter


@dataclass(frozen=True)
class ExecutedToolCall:
    """Result and correlation metadata for one attempted tool call."""

    tool_call_id: str
    name: str
    execution: ToolExecution
    duration_ms: int


class Executor:
    """Parse, execute, and trace tool calls produced by a planner.

    Args:
        tools: Dispatcher that owns the workspace and shared tool state.
        trace: Append-only event sink for debugging and replay.
        emit: Function used for concise interactive status messages.
    """

    def __init__(
        self,
        *,
        tools: ToolRunner,
        trace: TraceWriter,
        emit: Callable[[str], None] = print,
    ) -> None:
        self.tools = tools
        self.trace = trace
        self.emit = emit

    def execute_batch(
        self,
        step: int,
        tool_calls: tuple[dict[str, Any], ...],
        *,
        rejection_error: str | None = None,
        rejection_error_code: str | None = None,
        session_id: str | None = None,
        session_step: int | None = None,
        phase: str | None = None,
    ) -> list[ExecutedToolCall]:
        """Execute a validated batch while preserving model call order.

        Args:
            step: One-based agent-loop step used for trace correlation.
            tool_calls: Calls from the current assistant response.
            rejection_error: If set, reject every call without side effects.
            rejection_error_code: Stable code attached to every rejection result.

        Returns:
            One result per input call in the same order.
        """

        metadata = _session_metadata(session_id, session_step, phase)
        if rejection_error is not None:
            self.trace.write(
                "protocol_violation",
                {
                    "step": step,
                    "error_code": rejection_error_code,
                    "error": rejection_error,
                    "call_count": len(tool_calls),
                    **metadata,
                },
            )
            return [
                self._rejected_call(
                    step,
                    call,
                    rejection_error,
                    error_code=rejection_error_code,
                    metadata=metadata,
                )
                for call in tool_calls
            ]
        return [
            self._execute_tool_call(step, call, metadata=metadata)
            for call in tool_calls
        ]

    def _rejected_call(
        self,
        step: int,
        call: dict[str, Any],
        error: str,
        *,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutedToolCall:
        call_id = _tool_call_id(call, step)
        name = tool_name(call)
        data = {"ok": False, "error": error}
        if error_code is not None:
            data["error_code"] = error_code
        execution = ToolExecution(data)
        self.trace.write(
            "tool_call",
            {
                "step": step,
                "tool_call_id": call_id,
                "tool": name,
                "rejected": True,
                "error_code": error_code,
                **(metadata or {}),
            },
        )
        self._trace_result(
            step, call_id, name, execution, duration_ms=0, metadata=metadata
        )
        return ExecutedToolCall(call_id, name, execution, 0)

    def _execute_tool_call(
        self,
        step: int,
        call: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutedToolCall:
        call_id = _tool_call_id(call, step)
        name = tool_name(call)
        raw_arguments = (call.get("function") or {}).get("arguments", "{}")
        self.emit(f"Tool: {name}")
        try:
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise ValueError("tool arguments must be a JSON object")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must decode to an object")
        except (json.JSONDecodeError, ValueError) as exc:
            execution = ToolExecution(
                {"ok": False, "error": f"invalid tool arguments: {exc}"}
            )
            self.trace.write(
                "tool_call",
                {
                    "step": step,
                    "tool_call_id": call_id,
                    "tool": name,
                    "raw_arguments": raw_arguments,
                    **(metadata or {}),
                },
            )
            self._trace_result(
                step, call_id, name, execution, duration_ms=0, metadata=metadata
            )
            return ExecutedToolCall(call_id, name, execution, 0)

        self.trace.write(
            "tool_call",
            {
                "step": step,
                "tool_call_id": call_id,
                "tool": name,
                "arguments": arguments,
                **(metadata or {}),
            },
        )
        started = time.monotonic()
        execution = self.tools.execute(name, arguments)
        duration_ms = round((time.monotonic() - started) * 1000)
        status = "ok" if execution.data.get("ok") else "error"
        self.emit(f"  -> {status}")
        if not execution.data.get("ok"):
            self.emit(f"  {execution.data.get('error', 'unknown tool error')}")
        self._trace_result(
            step, call_id, name, execution, duration_ms, metadata=metadata
        )
        for event in execution.events:
            event_metadata = dict(metadata or {})
            event_metadata["phase"] = self.tools.context.workflow.phase.value
            self.trace.write(
                event.name,
                {"step": step, **event_metadata, **event.data},
            )
        return ExecutedToolCall(call_id, name, execution, duration_ms)

    def _trace_result(
        self,
        step: int,
        call_id: str,
        name: str,
        execution: ToolExecution,
        duration_ms: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.trace.write(
            "tool_result",
            {
                "step": step,
                "tool_call_id": call_id,
                "tool": name,
                "duration_ms": duration_ms,
                "result": execution.data,
                **(metadata or {}),
            },
        )


def tool_name(call: dict[str, Any]) -> str:
    """Return a normalized tool name from an OpenAI-compatible call object."""

    name = (call.get("function") or {}).get("name")
    return str(name) if name else "<missing>"


def _tool_call_id(call: dict[str, Any], step: int) -> str:
    return str(call.get("id") or f"missing-tool-call-id-{step}")


def _session_metadata(
    session_id: str | None, session_step: int | None, phase: str | None
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "session_id": session_id,
            "session_step": session_step,
            "phase": phase,
        }.items()
        if value is not None
    }
