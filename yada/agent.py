"""The minimal Yada agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from yada.client import Completion
from yada.prompts import SYSTEM_PROMPT, task_prompt
from yada.tools import ToolExecution, ToolRunner
from yada.trace import TraceWriter


class CompletionClient(Protocol):
    model: str

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion: ...


@dataclass(frozen=True)
class AgentResult:
    finished: bool
    steps: int
    summary: str
    usage: dict[str, int]
    final_state: dict[str, Any]


class Agent:
    def __init__(
        self,
        *,
        client: CompletionClient,
        tools: ToolRunner,
        trace: TraceWriter,
        max_steps: int = 30,
        emit: Callable[[str], None] = print,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.client = client
        self.tools = tools
        self.trace = trace
        self.max_steps = max_steps
        self.emit = emit

    def run(self, task: str) -> AgentResult:
        if not task.strip():
            raise ValueError("task must not be empty")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt(task)},
        ]
        total_usage: dict[str, int] = {}
        self.trace.write(
            "run_start",
            {
                "model": getattr(self.client, "model", "unknown"),
                "task": task,
                "workspace": str(self.tools.workspace.root),
                "max_steps": self.max_steps,
            },
        )

        no_tool_turns = 0
        for step in range(1, self.max_steps + 1):
            self.emit(f"\n[{step}/{self.max_steps}] Asking DeepSeek...")
            completion = self.client.complete(messages=messages, tools=self.tools.schemas)
            _merge_usage(total_usage, completion.usage)
            assistant_message = completion.message
            messages.append(assistant_message)
            self.trace.write(
                "assistant",
                {
                    "step": step,
                    "message": assistant_message,
                    "usage": completion.usage,
                    "response_id": completion.response_id,
                    "model": completion.model,
                    "system_fingerprint": completion.system_fingerprint,
                    "finish_reason": completion.finish_reason,
                },
            )

            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                no_tool_turns += 1
                content = assistant_message.get("content") or ""
                if content:
                    self.emit(f"DeepSeek: {content.strip()}")
                reminder = (
                    "Continue working with tools. You must call finish after a patch and a "
                    "successful test/build; a text-only response does not complete the task."
                )
                if no_tool_turns >= 3:
                    reminder += " This is your final reminder to use the required tool protocol."
                messages.append({"role": "user", "content": reminder})
                self.trace.write("protocol_reminder", {"step": step, "text": reminder})
                continue
            no_tool_turns = 0

            if len(tool_calls) > 1 and any(
                _tool_name(call) == "finish" for call in tool_calls
            ):
                executions = [
                    ToolExecution(
                        {
                            "ok": False,
                            "error": "finish must be the only tool call in its assistant turn",
                        }
                    )
                    for _ in tool_calls
                ]
            else:
                executions = [
                    self._execute_tool_call(step, call) for call in tool_calls
                ]

            for call, execution in zip(tool_calls, executions, strict=True):
                tool_call_id = call.get("id")
                if not tool_call_id:
                    tool_call_id = f"missing-tool-call-id-{step}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": execution.content,
                    }
                )
                self.trace.write(
                    "tool_result",
                    {
                        "step": step,
                        "tool_call_id": tool_call_id,
                        "tool": _tool_name(call),
                        "result": execution.data,
                    },
                )
                if execution.finished:
                    summary = str(execution.data.get("summary", "Task completed"))
                    final_state = self.tools.final_state()
                    result = AgentResult(
                        finished=True,
                        steps=step,
                        summary=summary,
                        usage=total_usage,
                        final_state=final_state,
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

    def _execute_tool_call(self, step: int, call: dict[str, Any]) -> ToolExecution:
        name = _tool_name(call)
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
                {"step": step, "tool": name, "raw_arguments": raw_arguments},
            )
            return execution

        self.trace.write(
            "tool_call", {"step": step, "tool": name, "arguments": arguments}
        )
        execution = self.tools.execute(name, arguments)
        status = "ok" if execution.data.get("ok") else "error"
        self.emit(f"  -> {status}")
        if not execution.data.get("ok"):
            self.emit(f"  {execution.data.get('error', 'unknown tool error')}")
        return execution


def _tool_name(call: dict[str, Any]) -> str:
    function = call.get("function") or {}
    name = function.get("name")
    return str(name) if name else "<missing>"


def _merge_usage(total: dict[str, int], current: dict[str, Any]) -> None:
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
