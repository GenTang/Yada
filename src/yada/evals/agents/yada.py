"""In-process adapter for the native Yada agent loop."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from yada.agents import Agent
from yada.environments import CommandApprover, CommandExecutor, DockerCommandExecutor
from yada.evals.base import AgentRunResult, PreparedTask, RunBudget
from yada.evals.patches import collect_git_patch
from yada.models import CompletionClient, DeepSeekClient
from yada.tools import ToolRunner
from yada.traces import TraceWriter


class YadaAgentAdapter:
    """Expose Yada through the same contract as external agents."""

    name = "yada"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com",
        thinking: bool = True,
        reasoning_effort: str = "max",
        api_timeout_seconds: int = 300,
        command_timeout_seconds: int = 120,
        command_policy: str = "ask",
        trace_level: str = "summary",
        client_factory: Callable[[RunBudget], CompletionClient] | None = None,
        emit: Callable[[str], None] = print,
    ) -> None:
        if not api_key and client_factory is None:
            raise ValueError("DEEPSEEK_API_KEY is required for the Yada adapter")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.api_timeout_seconds = api_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.command_policy = command_policy
        self.trace_level = trace_level
        self.client_factory = client_factory
        self.emit = emit

    def run(
        self,
        prepared: PreparedTask,
        budget: RunBudget,
        run_dir: Path,
    ) -> AgentRunResult:
        trace_path = run_dir / "yada-trace.jsonl"
        client = (
            self.client_factory(budget)
            if self.client_factory is not None
            else DeepSeekClient(
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                thinking=self.thinking,
                reasoning_effort=self.reasoning_effort,
                max_output_tokens=budget.max_output_tokens,
                timeout_seconds=self.api_timeout_seconds,
            )
        )
        tools = ToolRunner(
            prepared.workspace,
            approver=CommandApprover(self.command_policy),
            command_timeout_seconds=self.command_timeout_seconds,
            command_environment=_task_environment(prepared),
            command_executor=_task_command_executor(prepared),
        )
        command_provenance = _command_provenance(prepared, tools)
        agent = Agent(
            client=client,
            tools=tools,
            trace=TraceWriter(
                trace_path,
                level=self.trace_level,
            ),
            max_steps=budget.max_steps,
            emit=self.emit,
            trace_metadata={
                "case_id": prepared.task.instance_id,
                **command_provenance,
            },
        )

        started = time.monotonic()
        try:
            try:
                native_result = agent.run(prepared.task.problem_statement)
                status = "completed" if native_result.finished else "unfinished"
                summary = native_result.summary
                usage = native_result.usage
                steps = native_result.steps
                details = {
                    "finished": native_result.finished,
                    **command_provenance,
                }
            except Exception as exc:
                status = "error"
                summary = f"{type(exc).__name__}: {exc}"
                usage = {}
                steps = None
                details = {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    **command_provenance,
                }
        finally:
            tools.close()

        return AgentRunResult(
            agent=self.name,
            model=getattr(client, "model", self.model),
            status=status,
            patch=collect_git_patch(prepared.workspace),
            duration_ms=round((time.monotonic() - started) * 1000),
            steps=steps,
            usage=usage,
            summary=summary,
            trace_path=str(trace_path),
            details=details,
        )


def _task_environment(prepared: PreparedTask) -> dict[str, str]:
    value = prepared.metadata.get("environment", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _task_command_executor(prepared: PreparedTask) -> CommandExecutor | None:
    value = prepared.metadata.get("command_backend")
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") != "docker":
        raise ValueError("prepared command_backend must have type='docker'")
    image = value.get("image")
    workdir = value.get("workdir", "/testbed")
    platform = value.get("platform")
    if not isinstance(image, str) or not image.strip():
        raise ValueError("Docker command backend requires an image")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise ValueError("Docker command backend requires an absolute workdir")
    if platform is not None and not isinstance(platform, str):
        raise ValueError("Docker command backend platform must be a string")
    return DockerCommandExecutor(
        image,
        container_workspace=workdir,
        platform=platform,
    )


def _command_provenance(
    prepared: PreparedTask,
    tools: ToolRunner,
) -> dict[str, str]:
    provenance = {"command_environment": tools.context.command_executor.name}
    backend = prepared.metadata.get("command_backend")
    if isinstance(backend, dict) and isinstance(backend.get("image"), str):
        provenance["command_image"] = backend["image"]
    return provenance
