from __future__ import annotations

import subprocess
from pathlib import Path

from yada.environments import CommandApprover
from yada.tools import ToolRunner

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42
"""

RECOUNT_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,20 +1,30 @@
 def answer():
-    return 41
+    return 42
"""

CONTEXT_MISMATCH_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 99
+    return 42
"""

MALFORMED_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
?def answer():
"""

NEW_FILE_PATCH = """diff --git a/new_module.py b/new_module.py
new file mode 100644
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1 @@
+VALUE = 7
"""


def test_editing_strategy_freezes_public_tool_interface(
    git_workspace: Path,
) -> None:
    patch_only = ToolRunner(
        git_workspace,
        approver=CommandApprover("allow"),
    )
    replace_first = ToolRunner(
        git_workspace,
        approver=CommandApprover("allow"),
        editing_strategy="replace-first",
    )

    assert patch_only.editing_strategy.value == "patch-only"
    assert "apply_patch" in patch_only.tool_names
    assert "replace_text" not in patch_only.tool_names
    assert replace_first.editing_strategy.value == "replace-first"
    assert "apply_patch" in replace_first.tool_names
    assert "replace_text" in replace_first.tool_names
    assert patch_only.schemas is patch_only.schemas
    assert replace_first.schemas is replace_first.schemas
    assert not patch_only.execute("replace_text", {"edits": []}).data["ok"]


def test_read_and_hash_checked_patch(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    read = tool_runner.execute("read_file", {"path": "app.py"})
    assert read.data["ok"]

    applied = tool_runner.execute(
        "apply_patch",
        {
            "patch": PATCH,
            "expected_files": [{"path": "app.py", "sha256": read.data["sha256"]}],
        },
    )

    assert applied.data["ok"], applied.data
    assert "return 42" in (git_workspace / "app.py").read_text()


def test_recount_repairs_hunk_counts_in_check_and_apply(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")
    tool_runner.context.state.verified_revision = 0

    applied = tool_runner.execute(
        "apply_patch",
        {
            "patch": RECOUNT_PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )

    assert applied.data["ok"], applied.data
    assert "return 42" in (git_workspace / "app.py").read_text()
    assert tool_runner.context.state.revision == 1
    assert tool_runner.context.state.patch_count == 1
    assert tool_runner.context.state.verified_revision == -1


def test_stale_hash_rejected(git_workspace: Path, tool_runner: ToolRunner) -> None:
    rejected = tool_runner.execute(
        "apply_patch",
        {
            "patch": PATCH,
            "expected_files": [{"path": "app.py", "sha256": "0" * 64}],
        },
    )

    assert not rejected.data["ok"]
    assert rejected.data["error_code"] == "stale_hash"
    assert "stale file hash" in rejected.data["error"]
    assert rejected.data["details"]["paths"] == ["app.py"]
    assert rejected.data["details"]["current_sha256"] == tool_runner.workspace.sha256(
        git_workspace / "app.py"
    )
    assert "return 41" in (git_workspace / "app.py").read_text()


def test_patch_context_mismatch_is_structured_and_does_not_change_state(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")
    tool_runner.context.state.verified_revision = 0

    rejected = tool_runner.execute(
        "apply_patch",
        {
            "patch": CONTEXT_MISMATCH_PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )

    assert not rejected.data["ok"]
    assert rejected.data["error_code"] == "patch_context_mismatch"
    assert rejected.data["details"]["paths"] == ["app.py"]
    assert rejected.data["details"]["phase"] == "check"
    assert (git_workspace / "app.py").read_text() == "def answer():\n    return 41\n"
    assert tool_runner.context.state.revision == 0
    assert tool_runner.context.state.patch_count == 0
    assert tool_runner.context.state.verified_revision == 0


def test_malformed_patch_returns_invalid_patch(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")

    rejected = tool_runner.execute(
        "apply_patch",
        {
            "patch": MALFORMED_PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )

    assert not rejected.data["ok"]
    assert rejected.data["error_code"] == "invalid_patch"
    assert len(rejected.content) < 4_000
    assert (git_workspace / "app.py").read_text() == "def answer():\n    return 41\n"


def test_protected_and_symlink_patch_targets_are_structured(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    protected_patch = """diff --git a/.git/config b/.git/config
--- a/.git/config
+++ b/.git/config
@@ -1 +1 @@
-old
+new
"""
    (git_workspace / "app-link.py").symlink_to("app.py")
    symlink_patch = """diff --git a/app-link.py b/app-link.py
--- a/app-link.py
+++ b/app-link.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42
"""
    escaping_patch = """diff --git a/../outside.py b/../outside.py
--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""

    protected = tool_runner.execute(
        "apply_patch",
        {
            "patch": protected_patch,
            "expected_files": [{"path": ".git/config", "sha256": "NEW"}],
        },
    )
    symlink = tool_runner.execute(
        "apply_patch",
        {
            "patch": symlink_patch,
            "expected_files": [{"path": "app-link.py", "sha256": "0" * 64}],
        },
    )
    escaping = tool_runner.execute(
        "apply_patch",
        {
            "patch": escaping_patch,
            "expected_files": [{"path": "../outside.py", "sha256": "NEW"}],
        },
    )

    assert protected.data["error_code"] == "unsupported_target"
    assert symlink.data["error_code"] == "unsupported_target"
    assert escaping.data["error_code"] == "unsupported_target"
    assert "return 41" in (git_workspace / "app.py").read_text()


def test_multi_file_check_failure_does_not_partially_apply(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    second = git_workspace / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42
diff --git a/second.py b/second.py
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-VALUE = 99
+VALUE = 2
"""

    rejected = tool_runner.execute(
        "apply_patch",
        {
            "patch": patch,
            "expected_files": [
                {
                    "path": "app.py",
                    "sha256": tool_runner.workspace.sha256(git_workspace / "app.py"),
                },
                {"path": "second.py", "sha256": tool_runner.workspace.sha256(second)},
            ],
        },
    )

    assert rejected.data["error_code"] == "patch_context_mismatch"
    assert (git_workspace / "app.py").read_text() == "def answer():\n    return 41\n"
    assert second.read_text() == "VALUE = 1\n"
    assert tool_runner.context.state.revision == 0


def test_post_check_failure_returns_apply_failed(
    monkeypatch, git_workspace: Path, tool_runner: ToolRunner
) -> None:
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args[0], 0, "", "")
        return subprocess.CompletedProcess(args[0], 1, "", "simulated apply failure")

    monkeypatch.setattr("yada.tools.patch.subprocess.run", fake_run)
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")

    rejected = tool_runner.execute(
        "apply_patch",
        {
            "patch": PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )

    assert rejected.data["error_code"] == "apply_failed"
    assert rejected.data["details"]["phase"] == "apply"
    assert tool_runner.context.state.revision == 0


def test_new_file_uses_new_sentinel_and_appears_in_final_diff(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    applied = tool_runner.execute(
        "apply_patch",
        {
            "patch": NEW_FILE_PATCH,
            "expected_files": [{"path": "new_module.py", "sha256": "NEW"}],
        },
    )

    assert applied.data["ok"], applied.data
    assert (git_workspace / "new_module.py").read_text() == "VALUE = 7\n"
    assert "VALUE = 7" in tool_runner.final_state()["diff"]


def test_trace_directory_is_excluded_from_git_status(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    trace = git_workspace / ".yada" / "runs" / "trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("{}\n", encoding="utf-8")

    assert ".yada" not in tool_runner.final_state()["git_status"]


def test_workspace_escape_and_protected_paths_rejected(
    tool_runner: ToolRunner,
) -> None:
    escaped = tool_runner.execute("read_file", {"path": "../outside.txt"})
    protected = tool_runner.execute("read_file", {"path": ".git/config"})

    assert not escaped.data["ok"]
    assert not protected.data["ok"]


def test_finish_requires_verification_after_latest_patch(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")
    tool_runner.execute(
        "apply_patch",
        {
            "patch": PATCH,
            "expected_files": [{"path": "app.py", "sha256": digest}],
        },
    )

    premature = tool_runner.execute("finish", {"summary": "done"})
    assert not premature.data["ok"]

    checked = tool_runner.execute(
        "run_command",
        {
            "argv": ["python3", "-c", "import app; assert app.answer() == 42"],
            "purpose": "test",
        },
    )
    assert checked.data["ok"]
    assert checked.data["exit_code"] == 0

    finished = tool_runner.execute("finish", {"summary": "fixed answer"})
    assert finished.finished
    assert finished.data["ok"]


def test_command_environment_removes_api_keys(
    monkeypatch, tool_runner: ToolRunner
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "do-not-leak")

    result = tool_runner.execute(
        "run_command",
        {
            "argv": [
                "python3",
                "-c",
                "import os; print(os.getenv('DEEPSEEK_API_KEY', 'missing'))",
            ],
            "purpose": "inspect",
        },
    )

    assert result.data["ok"]
    assert result.data["stdout"].strip() == "missing"


def test_command_environment_accepts_non_secret_task_values(
    git_workspace: Path,
) -> None:
    runner = ToolRunner(
        git_workspace,
        approver=CommandApprover("allow"),
        command_environment={"YADA_CASE_MARKER": "ready"},
    )

    result = runner.execute(
        "run_command",
        {
            "argv": [
                "python3",
                "-c",
                "import os; print(os.environ['YADA_CASE_MARKER'])",
            ],
            "purpose": "inspect",
        },
    )

    assert result.data["ok"]
    assert result.data["stdout"].strip() == "ready"
