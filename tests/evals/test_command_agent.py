from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from yada.evals import EvalTask, PreparedTask, RunBudget
from yada.evals.agents import CommandAgentAdapter


def test_command_agent_applies_patch_only_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    patch = """diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; "
        f"Path(r'{{output_patch}}').write_text({patch!r}, encoding='utf-8')",
    ]
    adapter = CommandAgentAdapter(command, name="patch-writer")
    run_dir = tmp_path / "artifacts"
    run_dir.mkdir()

    result = adapter.run(
        PreparedTask(EvalTask("task-1", "Change VALUE"), workspace),
        RunBudget(),
        run_dir,
    )

    assert result.status == "completed"
    assert result.details["output_patch_applied"] is True
    assert (workspace / "value.py").read_text() == "VALUE = 2\n"
    assert "+VALUE = 2" in result.patch


def test_command_agent_receives_prepared_task_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "value.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    run_dir = tmp_path / "artifacts"
    run_dir.mkdir()
    adapter = CommandAgentAdapter(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['YADA_CASE_MARKER'])",
        ]
    )
    prepared = PreparedTask(
        EvalTask("task-env", "Inspect the environment"),
        workspace,
        {"environment": {"YADA_CASE_MARKER": "ready"}},
    )

    result = adapter.run(prepared, RunBudget(), run_dir)

    assert result.status == "completed"
    assert (run_dir / "agent.stdout.log").read_text().strip() == "ready"
