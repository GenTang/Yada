from __future__ import annotations

from pathlib import Path

from yada.tools import ToolRunner


PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42
"""

NEW_FILE_PATCH = """diff --git a/new_module.py b/new_module.py
new file mode 100644
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1 @@
+VALUE = 7
"""


def test_read_and_hash_checked_patch(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    read = tool_runner.execute("read_file", {"path": "app.py"})
    assert read.data["ok"]

    applied = tool_runner.execute(
        "apply_patch",
        {
            "patch": PATCH,
            "expected_files": [
                {"path": "app.py", "sha256": read.data["sha256"]}
            ],
        },
    )

    assert applied.data["ok"], applied.data
    assert "return 42" in (git_workspace / "app.py").read_text()


def test_stale_hash_rejected(git_workspace: Path, tool_runner: ToolRunner) -> None:
    rejected = tool_runner.execute(
        "apply_patch",
        {
            "patch": PATCH,
            "expected_files": [{"path": "app.py", "sha256": "0" * 64}],
        },
    )

    assert not rejected.data["ok"]
    assert "stale file hash" in rejected.data["error"]
    assert "return 41" in (git_workspace / "app.py").read_text()


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

