from __future__ import annotations

import subprocess
from pathlib import Path

from yada.environments import CommandApprover, DockerCommandExecutor
from yada.tools import ToolRunner


def test_docker_executor_mounts_workspace_and_routes_command(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "run":
            return subprocess.CompletedProcess(argv, 0, "container-id\n", "")
        if argv[1] == "exec":
            return subprocess.CompletedProcess(argv, 0, "inside\n", "")
        if argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr("yada.environments.commands.subprocess.run", fake_run)
    executor = DockerCommandExecutor(
        "swebench/agent-image:latest",
        platform="linux/x86_64",
    )
    runner = ToolRunner(
        tmp_path,
        approver=CommandApprover("allow"),
        command_executor=executor,
    )

    result = runner.execute(
        "run_command",
        {
            "argv": ["python", "-c", "print('ok')"],
            "purpose": "test",
        },
    )
    runner.close()

    assert result.data["ok"] is True
    assert result.data["stdout"] == "inside\n"
    assert result.data["environment"] == "docker"
    assert result.data["verified_revision"] == 0
    start = calls[0]
    assert start[:4] == ["docker", "run", "--platform", "linux/x86_64"]
    assert start[4:6] == ["--detach", "--rm"]
    assert any(
        item == f"type=bind,source={tmp_path.resolve()},target=/testbed"
        for item in start
    )
    execute = calls[1]
    assert execute[:2] == ["docker", "exec"]
    assert "conda activate testbed" in execute[-1]
    assert "exec python -c 'print('" in execute[-1]
    assert calls[-1][1:3] == ["rm", "--force"]


def test_local_executor_remains_the_default(tmp_path: Path) -> None:
    runner = ToolRunner(tmp_path, approver=CommandApprover("allow"))

    result = runner.execute(
        "run_command",
        {"argv": ["python", "-c", "print('local')"], "purpose": "inspect"},
    )

    assert result.data["ok"] is True
    assert result.data["stdout"].strip() == "local"
    assert result.data["environment"] == "local"
