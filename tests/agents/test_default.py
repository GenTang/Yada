from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

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
            tool_call("call-finish-task", "finish_task", {"summary": "fixed add"}),
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
    # DeepSeek requires the prior assistant reasoning_content on the next request.
    assert client.seen_messages[1][-2]["reasoning_content"] == "reasoning for read_file"


def test_step_limit_reports_verified_revision_without_finish(tmp_path: Path) -> None:
    path = tmp_path / "value.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=tmp_path, check=True)
    runner = ToolRunner(tmp_path, approver=CommandApprover("allow"))
    digest = runner.workspace.sha256(path)
    client = FakeClient(
        [
            tool_call(
                "replace",
                "replace_text",
                {
                    "edits": [
                        {
                            "path": "value.py",
                            "sha256": digest,
                            "old_text": "VALUE = 1",
                            "new_text": "VALUE = 2",
                        }
                    ]
                },
            ),
            tool_call(
                "test",
                "run_command",
                {
                    "argv": [
                        "python3",
                        "-c",
                        "import value; assert value.VALUE == 2",
                    ],
                    "purpose": "test",
                },
            ),
        ]
    )
    trace_path = tmp_path / ".yada" / "verified-step-limit.jsonl"

    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path),
        max_steps=2,
        emit=lambda _: None,
    ).run("Change VALUE")

    assert not result.finished
    assert result.summary == (
        "Step limit reached after verification succeeded but before finish_task was called."
    )
    run_end = read_trace(trace_path)[-1]
    assert run_end["event"] == "run_end"
    assert run_end["data"]["summary"] == result.summary


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
            run_id="debug-run",
        ),
        max_steps=2,
        emit=lambda _: None,
        trace_metadata={"case_id": "fixture-1"},
    )

    result = agent.run("Inspect VALUE")

    assert not result.finished
    assert result.summary == (
        "Step limit reached before the verification gate was satisfied."
    )
    events = read_trace(trace_path)
    assert reconstruct_model_request(events, 1) == client.seen_payloads[0]
    assert reconstruct_model_request(events, 2) == client.seen_payloads[1]
    second_messages = reconstruct_model_request(events, 2)["messages"]
    assert second_messages[-2]["reasoning_content"] == "reasoning for read_file"
    run_start = events[0]["data"]
    assert run_start["trace_level"] == "debug"
    assert run_start["editing_strategy"] == "replace-first"
    assert "apply_patch" in run_start["tool_names"]
    assert "replace_text" in run_start["tool_names"]
    assert run_start["provenance"]["case_id"] == "fixture-1"
    assert "yada_version" in run_start["provenance"]
    assert "workspace_base_commit" in run_start["provenance"]
    assert client.seen_payloads[0]["tools"] == client.seen_payloads[1]["tools"]


def test_planner_rejects_finish_task_mixed_with_other_calls() -> None:
    planner = Planner()
    assistant_message = {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "run_command", "arguments": "{}"}},
            {"function": {"name": "finish_task", "arguments": "{}"}},
        ],
    }

    plan = planner.plan(assistant_message, consecutive_text_turns=2)

    assert len(plan.tool_calls) == 2
    assert plan.consecutive_text_turns == 0
    assert plan.rejection_error == (
        "finish_task must be the only tool call in its assistant turn"
    )
    assert plan.rejection_error_code == "finish_task_must_be_alone"


def test_strategy_prompts_are_explicit_and_stable() -> None:
    patch_planner = Planner("patch-only")
    replace_planner = Planner("replace-first")

    patch_prompt = patch_planner.initial_messages("Fix it")[0]["content"]
    replace_prompt = replace_planner.initial_messages("Fix it")[0]["content"]

    assert "Use search when the target location is unclear" in replace_prompt
    assert "Never modify workspace files" in replace_prompt
    assert "Submit at most one editing" in patch_prompt
    assert "Submit at most one editing" in replace_prompt
    assert "inspect the directly relevant callers" in replace_prompt
    assert "wrapper must propagate its child process exit code" in replace_prompt
    assert "finish_task next." in replace_prompt
    assert "Do not perform final re-reads" in replace_prompt
    assert "Tool strategy:" not in patch_prompt
    assert "Tool strategy:" not in replace_prompt
    assert "Editing strategy: patch-only" in patch_prompt
    assert "Use apply_patch for every workspace edit" in patch_prompt
    assert "follow the structured recovery instruction" in patch_prompt
    assert "Editing strategy: replace-first" in replace_prompt
    assert "prefer replace_text with an exact" in replace_prompt
    assert "Once the target and intended edit are clear" in replace_prompt
    assert "Do not repeat" in replace_prompt
    assert "Retry or switch tools" in replace_prompt
    assert replace_prompt == replace_planner.initial_messages("Fix it")[0]["content"]


def test_agent_rejects_mismatched_strategy_components(tmp_path: Path) -> None:
    runner = ToolRunner(tmp_path, approver=CommandApprover("allow"))

    with pytest.raises(ValueError, match="same editing strategy"):
        Agent(
            client=FakeClient([]),
            tools=runner,
            trace=TraceWriter(None),
            planner=Planner("patch-only"),
        )


def test_planner_rejects_multiple_editing_operations() -> None:
    planner = Planner("replace-first")
    assistant_message = {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "replace_text", "arguments": "{}"}},
            {"function": {"name": "apply_patch", "arguments": "{}"}},
        ],
    }

    plan = planner.plan(assistant_message, consecutive_text_turns=0)

    assert len(plan.tool_calls) == 2
    assert plan.rejection_error_code == "multiple_edit_operations"
    assert plan.rejection_error == (
        "only one editing operation is allowed per assistant turn"
    )


def test_multiple_editing_calls_are_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "value.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=tmp_path, check=True)
    runner = ToolRunner(
        tmp_path,
        approver=CommandApprover("allow"),
        editing_strategy="replace-first",
    )
    digest = runner.workspace.sha256(path)
    patch = """diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    first = Completion(
        message={
            "role": "assistant",
            "content": "",
            "reasoning_content": "try replace and patch",
            "tool_calls": [
                {
                    "id": "replace",
                    "type": "function",
                    "function": {
                        "name": "replace_text",
                        "arguments": json.dumps(
                            {
                                "edits": [
                                    {
                                        "path": "value.py",
                                        "sha256": digest,
                                        "old_text": "VALUE = 1",
                                        "new_text": "VALUE = 2",
                                    }
                                ]
                            }
                        ),
                    },
                },
                {
                    "id": "patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps(
                            {
                                "patch": patch,
                                "expected_files": [
                                    {"path": "value.py", "sha256": digest}
                                ],
                            }
                        ),
                    },
                },
            ],
        },
        usage={},
        model="fake-deepseek-v4-pro",
        finish_reason="tool_calls",
    )
    client = FakeClient(
        [
            first,
            Completion(
                message={"role": "assistant", "content": "stop"},
                usage={},
                model="fake-deepseek-v4-pro",
                finish_reason="stop",
            ),
        ]
    )
    trace_path = tmp_path / ".yada" / "multiple-edits.jsonl"
    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path),
        max_steps=2,
        emit=lambda _: None,
    ).run("Change VALUE")

    assert not result.finished
    assert path.read_text(encoding="utf-8") == "VALUE = 1\n"
    tool_results = [
        message for message in client.seen_messages[1] if message.get("role") == "tool"
    ]
    assert len(tool_results) == 2
    assert all(
        json.loads(message["content"])["error_code"] == "multiple_edit_operations"
        for message in tool_results
    )
    events = read_trace(trace_path)
    violation = next(
        event for event in events if event["event"] == "protocol_violation"
    )
    assert violation["data"]["error_code"] == "multiple_edit_operations"
    assert runner.context.state.patch_count == 0


def test_failed_replace_is_observed_before_later_patch(tmp_path: Path) -> None:
    path = tmp_path / "value.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=tmp_path, check=True)
    runner = ToolRunner(
        tmp_path,
        approver=CommandApprover("allow"),
        editing_strategy="replace-first",
    )
    digest = runner.workspace.sha256(path)
    patch = """diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    client = FakeClient(
        [
            tool_call(
                "replace",
                "replace_text",
                {
                    "edits": [
                        {
                            "path": "value.py",
                            "sha256": digest,
                            "old_text": "VALUE = 9",
                            "new_text": "VALUE = 2",
                        }
                    ]
                },
            ),
            tool_call(
                "patch",
                "apply_patch",
                {
                    "patch": patch,
                    "expected_files": [{"path": "value.py", "sha256": digest}],
                },
            ),
        ]
    )
    trace_path = tmp_path / ".yada" / "fallback.jsonl"
    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path, level="debug"),
        max_steps=2,
        emit=lambda _: None,
    ).run("Change VALUE")

    assert not result.finished
    assert path.read_text(encoding="utf-8") == "VALUE = 2\n"
    observed = json.loads(client.seen_messages[1][-1]["content"])
    assert observed["error_code"] == "no_match"
    start = read_trace(trace_path)[0]["data"]
    assert start["editing_strategy"] == "replace-first"
    assert "replace_text" in start["tool_names"]


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
