from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yada.environments import CommandApprover
from yada.tools import ToolRunner


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True
    )
    return tmp_path


@pytest.fixture
def tool_runner(git_workspace: Path) -> ToolRunner:
    return ToolRunner(git_workspace, approver=CommandApprover("allow"))
