"""Adapter for any non-interactive external coding-agent command."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from yada.evals.base import AgentRunResult, PreparedTask, RunBudget
from yada.evals.patches import collect_git_patch


class CommandAgentAdapter:
    """Run an argv template without a shell and collect the resulting Git diff.

    Template items may use ``{task}``, ``{task_file}``, ``{workspace}``,
    ``{output_patch}``, and ``{run_dir}`` placeholders.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        name: str = "command",
        model: str = "unknown",
        environment: dict[str, str] | None = None,
    ) -> None:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("command argv must contain non-empty strings")
        self.argv = tuple(argv)
        self.name = name
        self.model = model
        self.environment = dict(environment or {})

    def run(
        self,
        prepared: PreparedTask,
        budget: RunBudget,
        run_dir: Path,
    ) -> AgentRunResult:
        task_file = run_dir / "public-task.md"
        task_file.write_text(prepared.task.problem_statement, encoding="utf-8")
        output_patch = run_dir / "agent.patch"
        substitutions = {
            "task": prepared.task.problem_statement,
            "task_file": str(task_file),
            "workspace": str(prepared.workspace),
            "output_patch": str(output_patch),
            "run_dir": str(run_dir),
        }
        argv = [_substitute(item, substitutions) for item in self.argv]
        environment = os.environ.copy()
        prepared_environment = prepared.metadata.get("environment", {})
        if isinstance(prepared_environment, dict):
            environment.update(
                {
                    str(key): str(value)
                    for key, value in prepared_environment.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            )
        environment.update(self.environment)

        started = time.monotonic()
        timed_out = False
        try:
            process = subprocess.run(
                argv,
                cwd=prepared.workspace,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=budget.wall_time_seconds,
                check=False,
            )
            return_code = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            return_code = 124
            stdout = _timeout_text(exc.stdout)
            stderr = _timeout_text(exc.stderr)

        stdout_path = run_dir / "agent.stdout.log"
        stderr_path = run_dir / "agent.stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        workspace_patch = collect_git_patch(prepared.workspace)
        proposed_patch = (
            output_patch.read_text(encoding="utf-8")
            if output_patch.is_file()
            else ""
        )
        patch_apply_error = ""
        output_patch_applied = False
        if proposed_patch.strip() and not workspace_patch.strip():
            applied = subprocess.run(
                ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                cwd=prepared.workspace,
                input=proposed_patch,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            if applied.returncode == 0:
                workspace_patch = collect_git_patch(prepared.workspace)
                output_patch_applied = True
            else:
                patch_apply_error = applied.stderr.strip() or "git apply failed"
        patch = workspace_patch or proposed_patch
        status = "completed" if return_code == 0 and not timed_out else "error"
        if patch_apply_error:
            status = "error"
        summary = (
            "external agent command completed"
            if status == "completed"
            else (
                f"external patch could not be applied: {patch_apply_error}"
                if patch_apply_error
                else f"external agent exited with status {return_code}"
            )
        )
        return AgentRunResult(
            agent=self.name,
            model=self.model,
            status=status,
            patch=patch,
            duration_ms=round((time.monotonic() - started) * 1000),
            summary=summary,
            details={
                "argv": argv,
                "return_code": return_code,
                "timed_out": timed_out,
                "output_patch_applied": output_patch_applied,
                "patch_apply_error": patch_apply_error or None,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            },
        )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _substitute(value: str, substitutions: dict[str, str]) -> str:
    for key, replacement in substitutions.items():
        value = value.replace("{" + key + "}", replacement)
    return value
