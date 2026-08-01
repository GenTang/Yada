"""Independent, policy-gated command execution."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from yada.exceptions import ToolError
from yada.tools.base import ToolContext
from yada.utils.text import timeout_text, truncate_text


ALLOWED_EXECUTABLES = {
    "bash",
    "cargo",
    "go",
    "git",
    "make",
    "mypy",
    "node",
    "nox",
    "npm",
    "pnpm",
    "poetry",
    "pyright",
    "pytest",
    "python",
    "python3",
    "ruff",
    "sh",
    "tox",
    "uv",
    "yarn",
}
SAFE_GIT_SUBCOMMANDS = {
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}


def run_command(
    context: ToolContext,
    argv: list[str],
    purpose: str,
    cwd: str = ".",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    if purpose not in {"inspect", "test", "build"}:
        raise ToolError("purpose must be inspect, test, or build")
    if not isinstance(argv, list) or not argv or len(argv) > 40:
        raise ToolError("argv must be a non-empty array with at most 40 items")
    if not all(isinstance(item, str) and item and "\0" not in item for item in argv):
        raise ToolError("every argv item must be a non-empty string without NUL bytes")
    executable = argv[0]
    if Path(executable).name != executable or executable not in ALLOWED_EXECUTABLES:
        raise ToolError(
            f"executable is not allowed: {executable}; allowed={sorted(ALLOWED_EXECUTABLES)}"
        )
    if executable in {"bash", "sh"} and "-c" in argv[1:]:
        raise ToolError("shell -c is disabled; pass a script path and arguments instead")
    if executable == "git" and (
        len(argv) < 2 or argv[1] not in SAFE_GIT_SUBCOMMANDS
    ):
        raise ToolError(
            f"only read-only git subcommands are allowed: {sorted(SAFE_GIT_SUBCOMMANDS)}"
        )

    command_cwd = context.workspace.resolve(cwd)
    if not command_cwd.is_dir():
        raise ToolError(f"command cwd is not a directory: {cwd}")
    display_cwd = context.workspace.display(command_cwd)
    if not context.approver.approve(argv, display_cwd):
        raise ToolError("command was denied by policy or user")

    effective_timeout = timeout_seconds or context.command_timeout_seconds
    if not isinstance(effective_timeout, int) or not 1 <= effective_timeout <= 1800:
        raise ToolError("timeout_seconds must be between 1 and 1800")
    env = _sanitized_environment()
    env["YADA_WORKSPACE"] = str(context.workspace.root)
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=command_cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
        )
        timed_out = False
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = timeout_text(exc.stdout)
        stderr = timeout_text(exc.stderr) + f"\nTimed out after {effective_timeout}s"
    duration_ms = round((time.monotonic() - started) * 1000)
    stdout_text, stdout_truncated = truncate_text(stdout, context.max_output_chars)
    stderr_text, stderr_truncated = truncate_text(stderr, context.max_output_chars)

    if purpose in {"test", "build"} and exit_code == 0 and not timed_out:
        context.state.verified_revision = context.state.revision
        context.state.successful_verifications.append(
            {
                "argv": argv,
                "purpose": purpose,
                "revision": context.state.revision,
                "duration_ms": duration_ms,
            }
        )
    return {
        "argv": argv,
        "cwd": display_cwd,
        "purpose": purpose,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "truncated": stdout_truncated or stderr_truncated,
        "verified_revision": (
            context.state.verified_revision
            if context.state.verified_revision >= 0
            else None
        ),
    }


def _sanitized_environment() -> dict[str, str]:
    secret_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in secret_markers)
    }

