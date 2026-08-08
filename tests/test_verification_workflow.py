from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from yada.agents import Agent
from yada.environments import CommandApprover, CommandResult
from yada.environments.commands import expand_workspace_environment
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
TEST_ERROR_PATCH = """diff --git a/tests/test_bug.py b/tests/test_bug.py
new file mode 100644
--- /dev/null
+++ b/tests/test_bug.py
@@ -0,0 +1,3 @@
+def test_bug(tmp_path, monkeypatch):
+    monkeypatch.chdir(tmp_path)
+    object().missing_test_helper()
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

    def run(
        self,
        *,
        workspace: Path,
        environment: dict[str, str],
        **_: Any,
    ) -> CommandResult:
        result = self.results.pop(0)
        write_fake_red_observation(workspace, environment, result)
        return result

    def close(self) -> None:
        pass


class MutatingExecutor(QueueExecutor):
    def __init__(
        self, results: list[CommandResult], *, mutate_on_calls: set[int]
    ) -> None:
        super().__init__(results)
        self.mutate_on_calls = mutate_on_calls
        self.calls = 0

    def run(self, *, workspace: Path, **kwargs: Any) -> CommandResult:
        self.calls += 1
        if self.calls in self.mutate_on_calls:
            (workspace / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        return super().run(workspace=workspace, **kwargs)


class RuntimeCheckingExecutor(QueueExecutor):
    def __init__(self, results: list[CommandResult]) -> None:
        super().__init__(results)
        self.runtime_contents: list[str] = []
        self.workspace_roots: list[Path] = []

    def run(self, *, workspace: Path, **kwargs: Any) -> CommandResult:
        self.workspace_roots.append(workspace)
        self.runtime_contents.append(
            (workspace / "generated_runtime.py").read_text(encoding="utf-8")
        )
        return super().run(workspace=workspace, **kwargs)


def write_fake_red_observation(
    workspace: Path,
    environment: dict[str, str],
    result: CommandResult,
) -> None:
    """Emulate the injected pytest plugin for deterministic executor tests."""

    expanded = expand_workspace_environment(environment, workspace.as_posix())
    report_value = expanded.get("YADA_RED_OBSERVER_REPORT")
    nonce = expanded.get("YADA_RED_OBSERVER_NONCE")
    target = expanded.get("YADA_RED_OBSERVER_TARGET")
    if not report_value or not nonce or not target:
        return
    combined = f"{result.stdout}\n{result.stderr}"
    folded = combined.casefold()
    events: list[dict[str, Any]] = []
    target_folded = target.casefold()
    target_failed = (
        f"failed {target_folded}" in folded or f"{target_folded} failed" in folded
    )
    target_passed = (
        result.exit_code == 0
        or f"passed {target_folded}" in folded
        or f"{target_folded} passed" in folded
    )
    if target_failed or target_passed:
        events.append({"event": "target_collected", "nodeid": target})
        events.append(
            {
                "event": "target_report",
                "nodeid": target,
                "when": "setup",
                "outcome": "passed",
                "wasxfail": False,
            }
        )
        if target_failed:
            exception_name = next(
                (
                    name
                    for name in ("AttributeError", "NameError", "TypeError")
                    if name.casefold() in folded
                ),
                "AssertionError",
            )
            events.append(
                {
                    "event": "target_report",
                    "nodeid": target,
                    "when": "call",
                    "outcome": "failed",
                    "wasxfail": False,
                    "exception_type": f"builtins.{exception_name}",
                    "traceback_paths": [target.split("::", 1)[0]],
                }
            )
        else:
            events.append(
                {
                    "event": "target_report",
                    "nodeid": target,
                    "when": "call",
                    "outcome": "passed",
                    "wasxfail": False,
                }
            )
        events.append(
            {
                "event": "target_report",
                "nodeid": target,
                "when": "teardown",
                "outcome": "passed",
                "wasxfail": False,
            }
        )
    elif any(marker in folded for marker in ("importerror", "syntaxerror")):
        events.append(
            {
                "event": "collection_error",
                "nodeid": target.split("::", 1)[0],
                "message": combined,
            }
        )
    events.append({"event": "session_finish", "exitstatus": result.exit_code})
    report = Path(report_value)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "".join(
            json.dumps(
                {**event, "schema_version": 1, "nonce": nonce},
                sort_keys=True,
            )
            + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


def red_observation(
    *,
    outcome: str,
    exception_type: str | None = None,
    traceback_paths: list[str] | None = None,
    exitstatus: int | None = None,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = [
        {"event": "target_collected", "nodeid": TEST_TARGET},
        {
            "event": "target_report",
            "nodeid": TEST_TARGET,
            "when": "setup",
            "outcome": "passed",
            "wasxfail": False,
        },
        {
            "event": "target_report",
            "nodeid": TEST_TARGET,
            "when": "call",
            "outcome": outcome,
            "wasxfail": False,
            "exception_type": exception_type,
            "traceback_paths": traceback_paths or [],
        },
        {
            "event": "target_report",
            "nodeid": TEST_TARGET,
            "when": "teardown",
            "outcome": "passed",
            "wasxfail": False,
        },
        {
            "event": "session_finish",
            "exitstatus": (
                exitstatus
                if exitstatus is not None
                else (1 if outcome == "failed" else 0)
            ),
        },
    ]
    return {
        "schema_version": 1,
        "status": "ok",
        "target": TEST_TARGET,
        "events": events,
    }


class FakeClient:
    model = "fake-deepseek-v4-pro"

    def __init__(self, completions: list[Completion]) -> None:
        self.completions = completions
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_tool_names: list[set[str]] = []

    def request_payload(self, *, messages, tools):
        return {"model": self.model, "messages": messages, "tools": tools}

    def complete(self, *, messages, tools):
        self.seen_messages.append(json.loads(json.dumps(messages)))
        self.seen_tool_names.append({schema["function"]["name"] for schema in tools})
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


def runtime_manifest(root: Path) -> dict[str, tuple[str, object, int]]:
    """Describe the runtime-visible tree while excluding Host control state."""

    manifest: dict[str, tuple[str, object, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in {".git", ".yada"} for part in relative.parts):
            continue
        mode = path.lstat().st_mode & 0o777
        key = relative.as_posix()
        if path.is_symlink():
            manifest[key] = ("symlink", path.readlink().as_posix(), mode)
        elif path.is_dir():
            manifest[key] = ("directory", "", mode)
        else:
            manifest[key] = ("file", path.read_bytes(), mode)
    return manifest


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
    assert red.data["exit_code"] == 2
    assert red.data["stdout"] == ""
    assert red.data["stderr"] == "ImportError while importing test module"
    assert red.data["truncated"] is False
    assert runner.context.workflow.phase.value == "red"
    assert not (workspace / "tests/test_bug.py").exists()
    runner.close()


def test_failed_red_output_remains_bounded(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        command_executor=QueueExecutor(
            [CommandResult(2, "", "ImportError: " + "x" * 200)]
        ),
        max_output_chars=30,
    )
    assert select_red(runner).data["ok"]
    assert apply_test_patch(runner).data["ok"]

    red = submit_red(runner)

    assert red.data["error_code"] == "red_import_error"
    assert red.data["truncated"] is True
    assert "characters omitted by Yada" in red.data["stderr"]
    assert "x" * 100 not in red.data["stderr"]
    runner.close()


def test_red_and_fix_use_the_same_frozen_complete_runtime_snapshot(
    tmp_path: Path,
) -> None:
    workspace = committed_workspace(tmp_path)
    (workspace / ".gitignore").write_text(
        "generated_runtime.py\nruntime/\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "ignore runtime"], cwd=workspace, check=True
    )
    (workspace / "generated_runtime.py").write_text(
        "RUNTIME_VALUE = 42\n", encoding="utf-8"
    )
    (workspace / "runtime" / "empty").mkdir(parents=True)
    executable = workspace / "runtime" / "tool.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (workspace / "runtime" / "version.py").symlink_to("../generated_runtime.py")
    (workspace / ".yada").mkdir()
    (workspace / ".yada" / "host-state").write_text("private\n", encoding="utf-8")
    expected_runtime = runtime_manifest(workspace)
    executor = RuntimeCheckingExecutor(
        [
            CommandResult(1, "FAILED tests/test_bug.py::test_bug\n1 failed", ""),
            CommandResult(0, "1 passed", ""),
        ]
    )
    runner = ToolRunner(
        workspace,
        approver=CommandApprover("allow"),
        command_executor=executor,
    )

    assert select_red(runner).data["ok"]
    assert runtime_manifest(runner.workspace.root) == expected_runtime
    assert not (runner.workspace.root / ".yada").exists()
    (workspace / "generated_runtime.py").write_text(
        "RUNTIME_VALUE = changed\n", encoding="utf-8"
    )
    assert apply_test_patch(runner).data["ok"]
    assert submit_red(runner).data["ok"]
    runner.context.workflow.start_fix()

    observed = runner.execute(
        "run_command",
        {
            "argv": ["pytest", "-q", TEST_TARGET],
            "purpose": "test",
            "verification_role": "target",
        },
    )

    assert observed.data["ok"]
    assert executor.runtime_contents == [
        "RUNTIME_VALUE = 42\n",
        "RUNTIME_VALUE = 42\n",
    ]
    assert len(set(executor.workspace_roots)) == 2
    assert workspace not in executor.workspace_roots
    assert (workspace / "generated_runtime.py").read_text() == (
        "RUNTIME_VALUE = changed\n"
    )
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
            "red_observation": red_observation(outcome="passed", exitstatus=1),
        },
    )

    assert status == "target_not_failed"
    assert "submitted target" in explanation


def test_red_uses_outer_structured_result_not_nested_pytest_output() -> None:
    status, explanation = classify_red_observation(
        target=TEST_TARGET,
        argv=["pytest", "-q", TEST_TARGET],
        result={
            "exit_code": 1,
            "timed_out": False,
            "stdout": (
                "collected 1 item\n"
                "Captured stdout call: collected 0 items; no tests ran\n"
                f"FAILED {TEST_TARGET} - AssertionError\n"
            ),
            "stderr": "",
            "red_observation": red_observation(
                outcome="failed",
                exception_type="builtins.AssertionError",
                traceback_paths=["tests/test_bug.py"],
            ),
        },
        test_paths=("tests/test_bug.py",),
    )

    assert status == "valid"
    assert "behaviorally" in explanation


def test_red_rejects_uncaught_test_authoring_error() -> None:
    status, explanation = classify_red_observation(
        target=TEST_TARGET,
        argv=["pytest", "-q", TEST_TARGET],
        result={
            "exit_code": 1,
            "timed_out": False,
            "stdout": f"FAILED {TEST_TARGET} - AttributeError",
            "stderr": "",
            "red_observation": red_observation(
                outcome="failed",
                exception_type="builtins.AttributeError",
                traceback_paths=["tests/test_bug.py"],
            ),
        },
        test_paths=("tests/test_bug.py",),
    )

    assert status == "red_test_error"
    assert "test code" in explanation


def test_red_accepts_exception_originating_in_production_code() -> None:
    status, _ = classify_red_observation(
        target=TEST_TARGET,
        argv=["pytest", "-q", TEST_TARGET],
        result={
            "exit_code": 1,
            "timed_out": False,
            "stdout": f"FAILED {TEST_TARGET} - AttributeError",
            "stderr": "",
            "red_observation": red_observation(
                outcome="failed",
                exception_type="builtins.AttributeError",
                traceback_paths=["tests/test_bug.py", "app.py"],
            ),
        },
        test_paths=("tests/test_bug.py",),
    )

    assert status == "valid"


def test_real_red_observer_rejects_test_code_attribute_error(tmp_path: Path) -> None:
    workspace = committed_workspace(tmp_path)
    runner = ToolRunner(workspace, approver=CommandApprover("allow"))
    assert select_red(runner).data["ok"]
    applied = runner.execute(
        "apply_patch",
        {
            "patch": TEST_ERROR_PATCH,
            "expected_files": [{"path": "tests/test_bug.py", "sha256": "NEW"}],
        },
    )
    assert applied.data["ok"]

    red = submit_red(runner)

    assert red.data["error_code"] == "red_test_error"
    assert red.data["red_observation"]["status"] == "ok"
    assert red.data["details"]["failure_kind"] == "test_error"
    assert red.data["details"]["exception_type"] == "builtins.AttributeError"
    assert not (runner.workspace.root / ".yada").exists()
    runner.close()


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
    first_red_messages = client.seen_messages[1]
    assert len(first_red_messages) == 2
    assert "Current phase: Red" in first_red_messages[0]["content"]
    assert "private reasoning for select_strategy" not in json.dumps(first_red_messages)
    first_fix_messages = client.seen_messages[3]
    assert len(first_fix_messages) == 2
    serialized = json.dumps(first_fix_messages)
    assert "private reasoning for submit_red_test" not in serialized
    assert "test_patch_sha256" in serialized
    assert "behavioral_assertion" in serialized
    assert client.seen_tool_names[0] == {
        "select_strategy",
        "search_code",
        "read_file",
    }
    assert "submit_red_test" in client.seen_tool_names[1]
    assert "run_command" not in client.seen_tool_names[1]
    assert "select_strategy" not in client.seen_tool_names[1]
    assert "finish_task" not in client.seen_tool_names[1]
    assert "finish_task" in client.seen_tool_names[3]
    assert "submit_red_test" not in client.seen_tool_names[3]

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
        "selection",
        "red",
        "fix",
    }
    html = render_trace_html(trace_path)
    assert "Verification workflow" in html
    assert "strategy_selected" in html
    assert "phase-red" in html
    assert "phase-fix" in html


def test_direct_execute_starts_a_fresh_phase_specific_session(tmp_path: Path) -> None:
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
    } == {"selection", "direct"}
    assert "select_strategy" in client.seen_tool_names[0]
    assert "select_strategy" not in client.seen_tool_names[1]
    assert "finish_task" in client.seen_tool_names[1]


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
