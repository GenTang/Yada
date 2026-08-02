from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from yada.agents import Agent, Planner
from yada.environments import CommandApprover
from yada.models import Completion
from yada.tools import ToolRunner
from yada.traces import TraceWriter, read_trace, reconstruct_model_request


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> Completion:
    return Completion(
        message={
            "role": "assistant",
            "content": "",
            "reasoning_content": f"reasoning for {name}",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        model="fake-deepseek-v4-pro",
        finish_reason="tool_calls",
        message_field_presence={
            "role_present": True,
            "content_present": True,
            "reasoning_content_present": True,
            "tool_calls_present": True,
        },
    )


class FakeClient:
    model = "fake-deepseek-v4-pro"

    def __init__(self, completions: list[Completion]) -> None:
        self.completions = completions
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_payloads: list[dict[str, Any]] = []

    def request_payload(self, *, messages, tools):
        return {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "enabled"},
        }

    def complete(self, *, messages, tools):
        self.seen_messages.append(list(messages))
        self.seen_payloads.append(
            json.loads(json.dumps(self.request_payload(messages=messages, tools=tools)))
        )
        return self.completions.pop(0)


def test_offline_end_to_end_loop(tmp_path: Path) -> None:
    path = tmp_path / "calc.py"
    path.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "calc.py"], cwd=tmp_path, check=True)
    runner = ToolRunner(tmp_path, approver=CommandApprover("allow"))
    digest = runner.workspace.sha256(path)
    patch = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
    client = FakeClient(
        [
            tool_call("call-read", "read_file", {"path": "calc.py"}),
            tool_call(
                "call-patch",
                "apply_patch",
                {
                    "patch": patch,
                    "expected_files": [{"path": "calc.py", "sha256": digest}],
                },
            ),
            tool_call(
                "call-test",
                "run_command",
                {
                    "argv": [
                        "python3",
                        "-c",
                        "import calc; assert calc.add(2, 3) == 5",
                    ],
                    "purpose": "test",
                },
            ),
            tool_call("call-finish", "finish", {"summary": "fixed add"}),
        ]
    )
    trace_path = tmp_path / ".yada" / "test.jsonl"
    agent = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path),
        max_steps=5,
        emit=lambda _: None,
    )

    result = agent.run("Fix calc.add")

    assert result.finished
    assert result.steps == 4
    assert "return a + b" in path.read_text()
    assert result.usage["prompt_tokens"] == 40
    trace = trace_path.read_text(encoding="utf-8")
    assert '"redacted": true' in trace
    assert "reasoning for read_file" not in trace
    assistant = next(
        event for event in read_trace(trace_path) if event["event"] == "assistant"
    )
    assert assistant["data"]["message_field_presence"]["content_present"] is True
    # DeepSeek requires the prior assistant reasoning_content on the next request.
    assert client.seen_messages[1][-2]["reasoning_content"] == "reasoning for read_file"


def test_debug_trace_reconstructs_exact_client_payload(tmp_path: Path) -> None:
    path = tmp_path / "value.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    runner = ToolRunner(tmp_path, approver=CommandApprover("allow"))
    client = FakeClient(
        [
            tool_call("call-read", "read_file", {"path": "value.py"}),
            Completion(
                message={"role": "assistant", "content": "still working"},
                usage={"prompt_tokens": 2, "completion_tokens": 1},
                model="fake-deepseek-v4-pro",
                finish_reason="stop",
            ),
        ]
    )
    trace_path = tmp_path / ".yada" / "debug.jsonl"
    agent = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(
            trace_path,
            level="debug",
            include_reasoning=True,
            run_id="debug-run",
        ),
        max_steps=2,
        emit=lambda _: None,
        trace_metadata={"case_id": "fixture-1"},
    )

    result = agent.run("Inspect VALUE")

    assert not result.finished
    events = read_trace(trace_path)
    assert reconstruct_model_request(events, 1) == client.seen_payloads[0]
    assert reconstruct_model_request(events, 2) == client.seen_payloads[1]
    second_messages = reconstruct_model_request(events, 2)["messages"]
    assert second_messages[-2]["reasoning_content"] == "reasoning for read_file"
    run_start = events[0]["data"]
    assert run_start["trace_level"] == "debug"
    assert run_start["provenance"]["case_id"] == "fixture-1"
    assert "yada_version" in run_start["provenance"]
    assert "workspace_base_commit" in run_start["provenance"]


def test_planner_rejects_finish_mixed_with_other_calls() -> None:
    planner = Planner()
    assistant_message = {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "run_command", "arguments": "{}"}},
            {"function": {"name": "finish", "arguments": "{}"}},
        ],
    }

    plan = planner.plan(assistant_message, consecutive_text_turns=2)

    assert len(plan.tool_calls) == 2
    assert plan.consecutive_text_turns == 0
    assert plan.rejection_error == (
        "finish must be the only tool call in its assistant turn"
    )


def test_planner_escalates_repeated_text_only_turns() -> None:
    planner = Planner()

    plan = planner.plan(
        {"role": "assistant", "content": "I am done."},
        consecutive_text_turns=2,
    )

    assert not plan.tool_calls
    assert plan.consecutive_text_turns == 3
    assert plan.display_text == "I am done."
    assert "final reminder" in (plan.reminder or "")
