from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from yada.agents import Agent
from yada.environments import CommandApprover
from yada.models import Completion
from yada.tools import ToolRunner
from yada.traces import TraceWriter


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

    def complete(self, *, messages, tools):
        self.seen_messages.append(list(messages))
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
    # DeepSeek requires the prior assistant reasoning_content on the next request.
    assert client.seen_messages[1][-2]["reasoning_content"] == "reasoning for read_file"

