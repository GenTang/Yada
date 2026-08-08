"""Pluggable command execution without adding runtime dependencies."""

from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from yada.exceptions import ToolError
from yada.utils.text import timeout_text

WORKSPACE_PATH_PLACEHOLDER = "{YADA_WORKSPACE}"


@dataclass(frozen=True)
class CommandResult:
    """Raw result returned by a local or container command backend."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandExecutor(Protocol):
    """Execute validated argv inside one prepared workspace environment."""

    name: str

    def run(
        self,
        *,
        argv: list[str],
        workspace: Path,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        """Execute one command and return its captured result."""

        ...

    def close(self) -> None:
        """Release resources owned by this executor."""

        ...


class LocalCommandExecutor:
    """Run commands directly on the host using a sanitized environment."""

    name = "local"

    def run(
        self,
        *,
        argv: list[str],
        workspace: Path,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        env = _sanitized_values(os.environ)
        env.update(
            expand_workspace_environment(environment, workspace.resolve().as_posix())
        )
        env["YADA_WORKSPACE"] = str(workspace)
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            return CommandResult(result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                124,
                timeout_text(exc.stdout),
                timeout_text(exc.stderr) + f"\nTimed out after {timeout_seconds}s",
                timed_out=True,
            )

    def close(self) -> None:
        """The host executor owns no persistent resources."""


class DockerCommandExecutor:
    """Run commands in a persistent container with the workspace bind-mounted."""

    name = "docker"

    def __init__(
        self,
        image: str,
        *,
        container_workspace: str = "/testbed",
        platform: str | None = None,
        docker_executable: str = "docker",
    ) -> None:
        if not image.strip():
            raise ValueError("Docker command image must not be empty")
        if not container_workspace.startswith("/"):
            raise ValueError("Docker workspace must be an absolute POSIX path")
        if platform is not None and not platform.strip():
            raise ValueError("Docker platform must not be empty")
        self.image = image
        self.container_workspace = container_workspace.rstrip("/") or "/"
        self.platform = platform
        self.docker_executable = docker_executable
        self.container_name = f"yada-agent-{uuid.uuid4().hex[:12]}"
        self._started_workspace: Path | None = None

    def run(
        self,
        *,
        argv: list[str],
        workspace: Path,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        root = workspace.resolve()
        self._ensure_started(root)
        relative = cwd.resolve().relative_to(root)
        container_cwd = PurePosixPath(self.container_workspace, relative.as_posix())
        command = [
            self.docker_executable,
            "exec",
            "--workdir",
            str(container_cwd),
            "--env",
            f"YADA_WORKSPACE={self.container_workspace}",
        ]
        expanded_environment = expand_workspace_environment(
            environment, self.container_workspace
        )
        for key, value in sorted(expanded_environment.items()):
            command.extend(["--env", f"{key}={value}"])
        script = (
            "if [ -f /opt/miniconda3/etc/profile.d/conda.sh ]; then "
            ". /opt/miniconda3/etc/profile.d/conda.sh && "
            "conda activate testbed >/dev/null 2>&1 || true; fi; "
            f"exec {shlex.join(argv)}"
        )
        command.extend(
            [
                self.container_name,
                "timeout",
                "--signal=TERM",
                str(timeout_seconds),
                "/bin/bash",
                "-c",
                script,
            ]
        )
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout_seconds + 10,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "Docker command execution requires the `docker` CLI"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                124,
                timeout_text(exc.stdout),
                timeout_text(exc.stderr) + f"\nTimed out after {timeout_seconds}s",
                timed_out=True,
            )
        return CommandResult(
            result.returncode,
            result.stdout,
            result.stderr,
            timed_out=result.returncode == 124,
        )

    def close(self) -> None:
        """Force-remove the ephemeral agent container, if it was started."""

        if self._started_workspace is None:
            return
        try:
            subprocess.run(
                [self.docker_executable, "rm", "--force", self.container_name],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._started_workspace = None

    def _ensure_started(self, workspace: Path) -> None:
        if self._started_workspace is not None:
            if self._started_workspace != workspace:
                raise ToolError("Docker executor cannot switch workspaces")
            return
        mount = f"type=bind,source={workspace},target={self.container_workspace}"
        command = [self.docker_executable, "run"]
        if self.platform is not None:
            command.extend(["--platform", self.platform])
        command.extend(
            [
                "--detach",
                "--rm",
                "--name",
                self.container_name,
                "--mount",
                mount,
                "--workdir",
                self.container_workspace,
                self.image,
                "tail",
                "-f",
                "/dev/null",
            ]
        )
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "Docker command execution requires the `docker` CLI"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError("timed out while starting the agent container") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ToolError(f"cannot start the agent container: {detail}")
        self._started_workspace = workspace


def _sanitized_values(values: Mapping[str, str]) -> dict[str, str]:
    secret_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        str(key): str(value)
        for key, value in values.items()
        if not any(marker in str(key).upper() for marker in secret_markers)
    }


def expand_workspace_environment(
    values: Mapping[str, str], workspace: str
) -> dict[str, str]:
    """Sanitize environment values and resolve Host-owned workspace placeholders."""

    return {
        key: value.replace(WORKSPACE_PATH_PLACEHOLDER, workspace)
        for key, value in _sanitized_values(values).items()
    }
