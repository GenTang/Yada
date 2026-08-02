from __future__ import annotations

import subprocess
from pathlib import Path

from yada.evals import EvalTask, PreparedTask, RunBudget
from yada.evals.agents import YadaAgentAdapter
from yada.models import Completion
from yada.traces import read_trace, reconstruct_model_request


class OneTurnClient:
    model = "fake-deepseek"

    def request_payload(self, *, messages, tools):
        return {"model": self.model, "messages": messages, "tools": tools}

    def complete(self, *, messages, tools):
        return Completion(
            message={"role": "assistant", "content": "inspect next"},
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
    assert start["provenance"]["case_id"] == "case-123"
    assert start["provenance"]["workspace_base_commit"] == head
    assert reconstruct_model_request(events, 1)["model"] == "fake-deepseek"
