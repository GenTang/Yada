from __future__ import annotations

import json
import subprocess
from pathlib import Path

from yada.agents.executor import Executor
from yada.environments import CommandApprover
from yada.evals import EvalTask, PreparedTask, RunBudget
from yada.evals.agents import YadaAgentAdapter
from yada.evals.agents.yada import _editing_metrics
from yada.models import Completion
from yada.tools import ToolRunner
from yada.traces import TraceWriter, read_trace, reconstruct_model_request


class OneTurnClient:
    model = "fake-deepseek"

    def __init__(self) -> None:
        self.seen_tools: list[list[dict[str, object]]] = []

    def request_payload(self, *, messages, tools):
        return {"model": self.model, "messages": messages, "tools": tools}

    def complete(self, *, messages, tools):
        self.seen_tools.append(tools)
        return Completion(
            message={
                "role": "assistant",
                "content": "inspect next",
                "reasoning_content": "debug reasoning",
            },
            usage={"prompt_tokens": 2, "completion_tokens": 1},
            model=self.model,
            finish_reason="stop",
        )


def test_yada_eval_trace_includes_case_and_workspace_provenance(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    run_dir = tmp_path / "artifacts"
    run_dir.mkdir()
    client = OneTurnClient()
    adapter = YadaAgentAdapter(
        client_factory=lambda _: client,
        trace_level="debug",
        editing_strategy="replace-first",
        emit=lambda _: None,
    )

    result = adapter.run(
        PreparedTask(EvalTask("case-123", "Inspect VALUE"), workspace),
        RunBudget(max_steps=1),
        run_dir,
    )

    assert result.status == "unfinished"
    events = read_trace(run_dir / "yada-trace.jsonl")
    start = events[0]["data"]
    assert start["editing_strategy"] == "replace-first"
    assert "replace_text" in start["tool_names"]
    assert start["provenance"]["case_id"] == "case-123"
    assert start["provenance"]["workspace_base_commit"] == head
    assert reconstruct_model_request(events, 1)["model"] == "fake-deepseek"
    assistant = next(event for event in events if event["event"] == "assistant")
    assert assistant["data"]["message"]["reasoning_content"] == "debug reasoning"
    initial_tool_names = {schema["function"]["name"] for schema in client.seen_tools[0]}
    assert initial_tool_names == {"select_strategy", "search_code", "read_file"}
    assert result.details["editing_strategy"] == "replace-first"
    metrics = result.details["editing_metrics"]
    assert metrics["first_edit_attempt_success"] is None
    assert metrics["edit_attempts"] == 0


def test_editing_metrics_capture_retry_and_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "value.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=workspace, check=True)
    tools = ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        editing_strategy="replace-first",
    )
    selected = tools.execute(
        "select_strategy",
        {"strategy": "direct_execute", "reason": "editing metric fixture"},
    )
    assert selected.data["ok"]
    digest = tools.workspace.sha256(path)
    trace_path = tmp_path / "metrics.jsonl"
    executor = Executor(
        tools=tools,
        trace=TraceWriter(trace_path),
        emit=lambda _: None,
    )

    def call(call_id: str, old_text: str) -> dict[str, object]:
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "replace_text",
                "arguments": json.dumps(
                    {
                        "edits": [
                            {
                                "path": "value.py",
                                "sha256": digest,
                                "old_text": old_text,
                                "new_text": "VALUE = 2",
                            }
                        ]
                    }
                ),
            },
        }

    executor.execute_batch(1, (call("miss", "VALUE = 9"),))
    executor.execute_batch(2, (call("success", "VALUE = 1"),))

    metrics = _editing_metrics(trace_path, tools)

    assert metrics["first_edit_attempt_success"] is False
    assert metrics["eventual_mutation_success"] is True
    assert metrics["edit_attempts"] == 2
    assert metrics["additional_edit_attempts"] == 1
    assert metrics["failed_edit_attempts"] == 1
    assert metrics["tool_attempts"] == {"apply_patch": 0, "replace_text": 2}
    assert metrics["error_codes"] == {"no_match": 1}
    assert metrics["verification_success_after_mutation"] is False
