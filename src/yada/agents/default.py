"""Default Yada orchestration loop.

The loop intentionally coordinates collaborators rather than implementing their
policies: :class:`Planner` decides what may happen, :class:`Executor` owns tool
side effects, and :class:`TraceWriter` records the resulting event stream.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from yada.agents.executor import Executor, tool_name
from yada.agents.planning import Planner
from yada.models import CompletionClient
from yada.tools import ToolRunner
from yada.traces import TraceWriter
from yada.traces.provenance import client_trace_config, collect_provenance


@dataclass(frozen=True)
class AgentResult:
    """Final outcome returned by :meth:`Agent.run`.

    Attributes:
        finished: Whether the verification-gated ``finish`` tool succeeded.
        steps: Number of model turns consumed.
        summary: Model-provided summary or the step-limit explanation.
        usage: Flattened token and provider usage counters.
        final_state: Git diff/status snapshot from the tool runner.
    """

    finished: bool
    steps: int
    summary: str
    usage: dict[str, int]
    final_state: dict[str, Any]


class Agent:
    """Coordinate model requests, planning decisions, and tool observations.

    Args:
        client: Model-neutral completion client.
        tools: Workspace-scoped tool dispatcher.
        trace: Trace sink for every important state transition.
        max_steps: Maximum number of model turns before returning unfinished.
        emit: Function used for concise interactive output.
        planner: Optional policy replacement for tests or experiments.
        executor: Optional execution replacement for tests or experiments.
        trace_metadata: Optional benchmark or caller metadata added to provenance.
    """

    def __init__(
        self,
        *,
        client: CompletionClient,
        tools: ToolRunner,
        trace: TraceWriter,
        max_steps: int = 30,
        emit: Callable[[str], None] = print,
        planner: Planner | None = None,
        executor: Executor | None = None,
        trace_metadata: dict[str, Any] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.client = client
        self.tools = tools
        self.trace = trace
        self.max_steps = max_steps
        self.emit = emit
        self.planner = planner or Planner()
        self.executor = executor or Executor(tools=tools, trace=trace, emit=emit)
        self.trace_metadata = dict(trace_metadata or {})

    def run(self, task: str) -> AgentResult:
        """Run a coding task until verified completion or the step limit.

        Args:
            task: Natural-language coding task. Whitespace-only input is rejected.

        Returns:
            An :class:`AgentResult` containing the completion state and final diff.

        Raises:
            ValueError: If ``task`` is empty.
            DeepSeekAPIError: Propagated from the configured completion client.
        """

        messages = self.planner.initial_messages(task)
        total_usage: dict[str, int] = {}
        self.trace.write(
            "run_start",
            {
                "model": getattr(self.client, "model", "unknown"),
                "task": task,
                "workspace": str(self.tools.workspace.root),
                "max_steps": self.max_steps,
                "trace_level": self.trace.level,
                "model_config": client_trace_config(self.client),
                "provenance": collect_provenance(
                    self.tools.workspace.root,
                    extra=self.trace_metadata,
                ),
            },
        )

        consecutive_text_turns = 0
        for step in range(1, self.max_steps + 1):
            self.emit(f"\n[{step}/{self.max_steps}] Asking DeepSeek...")
            context_metrics = _message_metrics(messages)
            request_id = f"{self.trace.run_id}:model:{step}"
            request_record: dict[str, Any] = {
                "step": step,
                "request_id": request_id,
                "context": context_metrics,
            }
            if self.trace.level == "debug":
                request_record["payload"] = _model_request_payload(
                    self.client,
                    messages,
                    self.tools.schemas,
                )
                request_record["capture"] = {
                    "reasoning": "included",
                    "secrets": "redacted",
                }
            self.trace.write(
                "model_request",
                request_record,
            )
            started = time.monotonic()
            try:
                completion = self.client.complete(
                    messages=messages, tools=self.tools.schemas
                )
            except Exception as exc:
                # Recording failures here leaves an explicit unmatched request in the
                # trace, which is far easier to diagnose than a silently truncated run.
                self.trace.write(
                    "model_error",
                    {
                        "step": step,
                        "request_id": request_id,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                raise
            duration_ms = round((time.monotonic() - started) * 1000)
            _merge_usage(total_usage, completion.usage)
            assistant_message = completion.message
            messages.append(assistant_message)
            assistant_record = {
                "step": step,
                "request_id": request_id,
                "duration_ms": duration_ms,
                "request_context": context_metrics,
                "message": assistant_message,
                "usage": completion.usage,
                "response_id": completion.response_id,
                "model": completion.model,
                "system_fingerprint": completion.system_fingerprint,
                "finish_reason": completion.finish_reason,
            }
            if completion.message_field_presence is not None:
                assistant_record["message_field_presence"] = (
                    completion.message_field_presence
                )
            self.trace.write(
                "assistant",
                assistant_record,
            )

            plan = self.planner.plan(
                assistant_message,
                consecutive_text_turns=consecutive_text_turns,
            )
            self.trace.write(
                "plan_decision",
                {
                    "step": step,
                    "action": "execute_tools" if plan.tool_calls else "remind",
                    "tools": [tool_name(call) for call in plan.tool_calls],
                    "rejection_error": plan.rejection_error,
                },
            )
            consecutive_text_turns = plan.consecutive_text_turns
            if not plan.tool_calls:
                if plan.display_text:
                    self.emit(f"DeepSeek: {plan.display_text}")
                if plan.reminder is None:
                    raise RuntimeError("planner returned no calls and no reminder")
                messages.append({"role": "user", "content": plan.reminder})
                self.trace.write(
                    "protocol_reminder", {"step": step, "text": plan.reminder}
                )
                continue

            executed_calls = self.executor.execute_batch(
                step,
                plan.tool_calls,
                rejection_error=plan.rejection_error,
            )
            for executed in executed_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": executed.tool_call_id,
                        "content": executed.execution.content,
                    }
                )
                if executed.execution.finished:
                    result = AgentResult(
                        finished=True,
                        steps=step,
                        summary=str(
                            executed.execution.data.get("summary", "Task completed")
                        ),
                        usage=total_usage,
                        final_state=self.tools.final_state(),
                    )
                    self.trace.write("run_end", _result_record(result))
                    return result

        result = AgentResult(
            finished=False,
            steps=self.max_steps,
            summary="Step limit reached before the verification gate was satisfied.",
            usage=total_usage,
            final_state=self.tools.final_state(),
        )
        self.trace.write("run_end", _result_record(result))
        return result


def _message_metrics(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Return cheap context-growth signals without tokenizing provider payloads."""

    encoded = json.dumps(messages, ensure_ascii=False, default=str)
    return {
        "message_count": len(messages),
        "serialized_chars": len(encoded),
        "tool_result_count": sum(
            1 for message in messages if message.get("role") == "tool"
        ),
    }


def _model_request_payload(
    client: CompletionClient,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the provider payload, with a model-neutral fallback for test clients."""

    builder = getattr(client, "request_payload", None)
    if callable(builder):
        payload = builder(messages=messages, tools=tools)
        if not isinstance(payload, dict):
            raise TypeError("completion client request_payload must return an object")
        return payload
    return {
        "model": getattr(client, "model", "unknown"),
        "messages": messages,
        "tools": tools,
    }


def _merge_usage(total: dict[str, int], current: dict[str, Any]) -> None:
    """Flatten and add integer usage counters reported by the provider."""

    for key, value in current.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, int):
                    composite = f"{key}.{nested_key}"
                    total[composite] = total.get(composite, 0) + nested_value


def _result_record(result: AgentResult) -> dict[str, Any]:
    return {
        "finished": result.finished,
        "steps": result.steps,
        "summary": result.summary,
        "usage": result.usage,
        "final_state": result.final_state,
    }
