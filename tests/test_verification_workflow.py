from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from yada.agents import Agent
from yada.environments import CommandApprover, CommandResult
from yada.models import Completion
from yada.tools import ToolRunner
from yada.traces import TraceWriter, read_trace, render_trace_html
from yada.verification import classify_red_observation

TEST_TARGET = "tests/test_bug.py::test_bug"
TEST_PATCH = """diff --git a/tests/test_bug.py b/tests/test_bug.py
new file mode 100644
--- /dev/null
+++ b/tests/test_bug.py
@@ -0,0 +1,2 @@
+def test_bug():
+    assert False
"""
FIX_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
SECOND_FIX_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 2
+VALUE = 3
"""


class QueueExecutor:
    name = "fake"

    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results

    def run(self, **_: Any) -> CommandResult:
        return self.results.pop(0)

    def close(self) -> None:
        pass


class MutatingExecutor(QueueExecutor):
    def __init__(
        self, results: list[CommandResult], *, mutate_on_calls: set[int]
    ) -> None:
        super().__init__(results)
        self.mutate_on_calls = mutate_on_calls
        self.calls = 0

    def run(self, *, workspace: Path, **_: Any) -> CommandResult:
        self.calls += 1
        if self.calls in self.mutate_on_calls:
            (workspace / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        return self.results.pop(0)


class FakeClient:
    model = "fake-deepseek-v4-pro"

    def __init__(self, completions: list[Completion]) -> None:
        self.completions = completions
        self.seen_messages: list[list[dict[str, Any]]] = []

    def request_payload(self, *, messages, tools):
        return {"model": self.model, "messages": messages, "tools": tools}

    def complete(self, *, messages, tools):
        self.seen_messages.append(json.loads(json.dumps(messages)))
        return self.completions.pop(0)


def call(call_id: str, name: str, arguments: dict[str, Any]) -> Completion:
    return Completion(
        message={
            "role": "assistant",
            "content": "",
            "reasoning_content": f"private reasoning for {name}",
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
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        model="fake-deepseek-v4-pro",
        finish_reason="tool_calls",
    )


def committed_workspace(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def runner_with_results(workspace: Path, results: list[CommandResult]) -> ToolRunner:
    return ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        command_executor=QueueExecutor(results),
    )


def select_red(runner: ToolRunner):
    return runner.execute(
        "select_strategy",
        {"strategy": "red_green", "reason": "reproducible regression"},
    )


def apply_test_patch(runner: ToolRunner):
    return runner.execute(
        "apply_patch",
        {
            "patch": TEST_PATCH,
            "expected_files": [{"path": "tests/test_bug.py", "sha256": "NEW"}],
        },
    )


def submit_red(runner: ToolRunner):
    return runner.execute(
        "submit_red_test",
        {"target": TEST_TARGET, "argv": ["pytest", "-q", TEST_TARGET]},
    )


def test_strategy_is_required_and_irreversible(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = runner_with_results(workspace, [])
    digest = runner.workspace.sha256(workspace / "app.py")

    rejected = runner.execute(
        "apply_patch",
        {
            "patch": FIX_PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )
    selected = runner.execute(
        "select_strategy",
        {"strategy": "direct_execute", "reason": "mechanical change"},
    )
    repeated = runner.execute(
        "select_strategy",
        {"strategy": "red_green", "reason": "changed my mind"},
    )

    assert rejected.data["error_code"] == "strategy_required"
    assert selected.data["ok"]
    assert repeated.data["error_code"] == "strategy_already_selected"


def test_failed_red_precondition_cannot_downgrade_to_direct(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    (workspace / "app.py").write_text("VALUE = dirty\n", encoding="utf-8")
    runner = runner_with_results(workspace, [])

    selected = select_red(runner)
    fallback = runner.execute(
        "select_strategy",
        {"strategy": "direct_execute", "reason": "fallback"},
    )

    assert selected.data["error_code"] == "baseline_unavailable"
    assert selected.stop_reason == "workflow_failed"
    assert fallback.data["error_code"] == "strategy_already_selected"


def test_red_rejects_production_edits_and_invalid_failures(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = runner_with_results(
        workspace,
        [CommandResult(2, "", "ImportError while importing test module")],
    )
    assert select_red(runner).data["ok"]
    digest = runner.workspace.sha256(workspace / "app.py")

    production = runner.execute(
        "apply_patch",
        {
            "patch": FIX_PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )
    assert production.data["error_code"] == "red_production_edit_rejected"
    assert apply_test_patch(runner).data["ok"]

    red = submit_red(runner)

    assert red.data["error_code"] == "red_import_error"
    assert runner.context.workflow.phase.value == "red"
    assert not (workspace / "tests/test_bug.py").exists()
    runner.close()


def test_red_requires_failure_attributed_to_the_exact_target() -> None:
    status, explanation = classify_red_observation(
        target=TEST_TARGET,
        argv=["pytest", "-q", TEST_TARGET, "tests/test_other.py::test_other"],
        result={
            "exit_code": 1,
            "timed_out": False,
            "stdout": (
                f"{TEST_TARGET} PASSED\n"
                "FAILED tests/test_other.py::test_other - AssertionError\n"
            ),
            "stderr": "",
        },
    )

    assert status == "target_not_failed"
    assert "submitted target" in explanation


def test_full_red_green_gate_and_frozen_test(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = runner_with_results(
        workspace,
        [
            CommandResult(1, "FAILED tests/test_bug.py::test_bug\n1 failed", ""),
            CommandResult(0, "1 passed", ""),
            CommandResult(0, "20 passed", ""),
            CommandResult(0, "1 passed", ""),
            CommandResult(0, "20 passed", ""),
        ],
    )
    assert select_red(runner).data["ok"]
    assert apply_test_patch(runner).data["ok"]

    red = submit_red(runner)

    assert red.data["ok"]
    assert red.stop_reason == "test_frozen"
    assert (workspace / "tests/test_bug.py").is_file()
    assert [event.name for event in red.events] == ["red_observed", "test_frozen"]

    workflow = runner.context.workflow
    workflow.start_fix()
    workflow.drain_events()
    test_digest = runner.workspace.sha256(workspace / "tests/test_bug.py")
    frozen_edit = runner.execute(
        "replace_text",
        {
            "edits": [
                {
                    "path": "tests/test_bug.py",
                    "sha256": test_digest,
                    "old_text": "assert False",
                    "new_text": "assert True",
                }
            ]
        },
    )
    assert frozen_edit.data["error_code"] == "frozen_test_edit_rejected"

    app_digest = runner.workspace.sha256(workspace / "app.py")
    fixed = runner.execute(
        "apply_patch",
        {
            "patch": FIX_PATCH,
            "expected_files": [{"path": "app.py", "sha256": app_digest}],
        },
    )
    assert fixed.data["ok"]

    green = runner.execute(
        "run_command",
        {
            "argv": ["pytest", "-q", TEST_TARGET],
            "purpose": "test",
            "verification_role": "target",
        },
    )
    regression = runner.execute(
        "run_command",
        {
            "argv": ["pytest", "-q", "tests"],
            "purpose": "test",
            "verification_role": "regression",
        },
    )
    second_digest = runner.workspace.sha256(workspace / "app.py")
    changed_after_green = runner.execute(
        "apply_patch",
        {
            "patch": SECOND_FIX_PATCH,
            "expected_files": [{"path": "app.py", "sha256": second_digest}],
        },
    )
    stale_finish = runner.execute("finish_task", {"summary": "stale evidence"})
    green_again = runner.execute(
        "run_command",
        {
            "argv": ["pytest", "-q", TEST_TARGET],
            "purpose": "test",
            "verification_role": "target",
        },
    )
    regression_again = runner.execute(
        "run_command",
        {
            "argv": ["pytest", "-q", "tests"],
            "purpose": "test",
            "verification_role": "regression",
        },
    )
    finished = runner.execute("finish_task", {"summary": "fixed with regression"})

    assert green.data["ok"]
    assert [event.name for event in green.events] == ["green_observed"]
    assert regression.data["ok"]
    assert [event.name for event in regression.events] == ["regression_verified"]
    assert changed_after_green.data["ok"]
    assert [event.name for event in changed_after_green.events] == [
        "verification_invalidated"
    ]
    assert stale_finish.data["ok"] is False
    assert "latest revision" in stale_finish.data["error"]
    assert green_again.data["ok"]
    assert regression_again.data["ok"]
    assert finished.finished
    assert [event.name for event in finished.events] == ["finish_accepted"]


def test_red_command_source_mutation_aborts_without_touching_primary(
    tmp_path: Path,
) -> None:
    workspace = committed_workspace(tmp_path)
    runner = ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        command_executor=MutatingExecutor(
            [CommandResult(1, "FAILED tests/test_bug.py::test_bug\n1 failed", "")],
            mutate_on_calls={1},
        ),
    )
    assert select_red(runner).data["ok"]
    assert apply_test_patch(runner).data["ok"]

    red = submit_red(runner)

    assert red.data["error_code"] == "red_production_change_detected"
    assert red.stop_reason == "workflow_failed"
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (workspace / "tests/test_bug.py").exists()
    runner.close()


def test_fix_command_mutation_is_discarded_and_cannot_count_as_green(
    tmp_path: Path,
) -> None:
    workspace = committed_workspace(tmp_path)
    runner = ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        command_executor=MutatingExecutor(
            [
                CommandResult(1, "FAILED tests/test_bug.py::test_bug\n1 failed", ""),
                CommandResult(0, "1 passed", ""),
            ],
            mutate_on_calls={2},
        ),
    )
    assert select_red(runner).data["ok"]
    assert apply_test_patch(runner).data["ok"]
    assert submit_red(runner).data["ok"]
    runner.context.workflow.start_fix()
    runner.context.workflow.drain_events()
    app_digest = runner.workspace.sha256(workspace / "app.py")
    assert runner.execute(
        "apply_patch",
        {
            "patch": FIX_PATCH,
            "expected_files": [{"path": "app.py", "sha256": app_digest}],
        },
    ).data["ok"]

    green = runner.execute(
        "run_command",
        {
            "argv": ["pytest", "-q", TEST_TARGET],
            "purpose": "test",
            "verification_role": "target",
        },
    )

    assert green.data["error_code"] == "verification_command_mutated_workspace"
    assert runner.context.workflow.green_revision == -1
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_agent_uses_fresh_fix_context_and_one_trace(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    executor = QueueExecutor(
        [
            CommandResult(1, "FAILED tests/test_bug.py::test_bug\n1 failed", ""),
            CommandResult(0, "1 passed", ""),
            CommandResult(0, "20 passed", ""),
        ]
    )
    runner = ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        command_executor=executor,
    )
    app_digest = runner.workspace.sha256(workspace / "app.py")
    client = FakeClient(
        [
            call(
                "strategy",
                "select_strategy",
                {"strategy": "red_green", "reason": "reproducible regression"},
            ),
            call(
                "test-patch",
                "apply_patch",
                {
                    "patch": TEST_PATCH,
                    "expected_files": [{"path": "tests/test_bug.py", "sha256": "NEW"}],
                },
            ),
            call(
                "red",
                "submit_red_test",
                {"target": TEST_TARGET, "argv": ["pytest", "-q", TEST_TARGET]},
            ),
            call(
                "fix",
                "apply_patch",
                {
                    "patch": FIX_PATCH,
                    "expected_files": [{"path": "app.py", "sha256": app_digest}],
                },
            ),
            call(
                "green",
                "run_command",
                {
                    "argv": ["pytest", "-q", TEST_TARGET],
                    "purpose": "test",
                    "verification_role": "target",
                },
            ),
            call(
                "regression",
                "run_command",
                {
                    "argv": ["pytest", "-q", "tests"],
                    "purpose": "test",
                    "verification_role": "regression",
                },
            ),
            call("finish", "finish_task", {"summary": "fixed regression"}),
        ]
    )
    trace_path = workspace / ".yada" / "runs" / "red-green.jsonl"

    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path, level="debug", run_id="red-green-run"),
        max_steps=8,
        emit=lambda _: None,
    ).run("Fix VALUE regression")

    assert result.finished
    first_fix_messages = client.seen_messages[3]
    assert len(first_fix_messages) == 2
    serialized = json.dumps(first_fix_messages)
    assert "private reasoning for submit_red_test" not in serialized
    assert "test_patch_sha256" in serialized

    events = read_trace(trace_path)
    workflow_events = [
        event["event"]
        for event in events
        if event["event"]
        in {
            "strategy_selected",
            "red_started",
            "red_observed",
            "test_frozen",
            "fix_started",
            "green_observed",
            "regression_verified",
            "finish_accepted",
        }
    ]
    assert workflow_events == [
        "strategy_selected",
        "red_started",
        "red_observed",
        "test_frozen",
        "fix_started",
        "green_observed",
        "regression_verified",
        "finish_accepted",
    ]
    assert {event["data"].get("session_id") for event in events} >= {
        "primary",
        "fix",
    }
    html = render_trace_html(trace_path)
    assert "Verification workflow" in html
    assert "strategy_selected" in html
    assert "phase-red" in html
    assert "phase-fix" in html


def test_direct_execute_continues_in_the_original_session(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = runner_with_results(workspace, [CommandResult(0, "ok", "")])
    digest = runner.workspace.sha256(workspace / "app.py")
    client = FakeClient(
        [
            call(
                "strategy",
                "select_strategy",
                {"strategy": "direct_execute", "reason": "mechanical change"},
            ),
            call(
                "fix",
                "apply_patch",
                {
                    "patch": FIX_PATCH,
                    "expected_files": [{"path": "app.py", "sha256": digest}],
                },
            ),
            call(
                "test",
                "run_command",
                {"argv": ["pytest", "-q"], "purpose": "test"},
            ),
            call("finish", "finish_task", {"summary": "direct change"}),
        ]
    )
    trace_path = workspace / ".yada" / "runs" / "direct.jsonl"

    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path),
        max_steps=4,
        emit=lambda _: None,
    ).run("Change VALUE")

    assert result.finished
    events = read_trace(trace_path)
    assert not any(event["event"] == "fix_started" for event in events)
    assert {
        event["data"].get("session_id")
        for event in events
        if event["event"] == "model_request"
    } == {"primary"}


def test_red_step_limit_does_not_materialize_unfrozen_test(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = runner_with_results(workspace, [])
    client = FakeClient(
        [
            call(
                "strategy",
                "select_strategy",
                {"strategy": "red_green", "reason": "reproducible regression"},
            ),
            call(
                "test-patch",
                "apply_patch",
                {
                    "patch": TEST_PATCH,
                    "expected_files": [{"path": "tests/test_bug.py", "sha256": "NEW"}],
                },
            ),
        ]
    )

    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(None),
        max_steps=2,
        emit=lambda _: None,
    ).run("Fix VALUE regression")

    assert not result.finished
    assert result.summary.startswith("Red phase reached the step limit")
    assert not (workspace / "tests/test_bug.py").exists()
    runner.close()


def test_valid_red_on_last_step_materializes_test_but_does_not_start_fix(
    tmp_path: Path,
) -> None:
    workspace = committed_workspace(tmp_path)
    runner = runner_with_results(
        workspace,
        [CommandResult(1, "FAILED tests/test_bug.py::test_bug\n1 failed", "")],
    )
    client = FakeClient(
        [
            call(
                "strategy",
                "select_strategy",
                {"strategy": "red_green", "reason": "reproducible regression"},
            ),
            call(
                "test-patch",
                "apply_patch",
                {
                    "patch": TEST_PATCH,
                    "expected_files": [{"path": "tests/test_bug.py", "sha256": "NEW"}],
                },
            ),
            call(
                "red",
                "submit_red_test",
                {"target": TEST_TARGET, "argv": ["pytest", "-q", TEST_TARGET]},
            ),
        ]
    )
    trace_path = workspace / ".yada" / "runs" / "budget.jsonl"

    result = Agent(
        client=client,
        tools=runner,
        trace=TraceWriter(trace_path),
        max_steps=3,
        emit=lambda _: None,
    ).run("Fix VALUE regression")

    assert not result.finished
    assert result.summary.startswith("Valid Red evidence was frozen")
    assert (workspace / "tests/test_bug.py").is_file()
    assert trace_path.with_suffix(".red-test.patch").is_file()
    assert "fix_started" not in {event["event"] for event in read_trace(trace_path)}
