"""Stable data contracts shared by evaluation engines and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

EVAL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RunBudget:
    """Comparable limits supplied to every agent adapter."""

    max_steps: int = 30
    wall_time_seconds: int = 1_800
    max_output_tokens: int = 16_384

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.wall_time_seconds < 1:
            raise ValueError("wall_time_seconds must be positive")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True)
class EvalTask:
    """Public task information that may be shown to an agent."""

    instance_id: str
    problem_statement: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("instance_id must not be empty")
        if not self.problem_statement.strip():
            raise ValueError("problem_statement must not be empty")


@dataclass(frozen=True)
class PreparedTask:
    """A public task paired with an isolated candidate workspace."""

    task: EvalTask
    workspace: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunResult:
    """Normalized output produced by any coding-agent implementation."""

    agent: str
    model: str
    status: str
    patch: str
    duration_ms: int
    steps: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    summary: str = ""
    trace_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"completed", "unfinished", "error"}:
            raise ValueError(f"invalid agent status: {self.status}")


@dataclass(frozen=True)
class GradeResult:
    """Benchmark-owned verdict, independent of the agent's self-report."""

    status: str
    resolved: bool | None
    duration_ms: int
    tests_passed: int | None = None
    tests_failed: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        allowed = {"resolved", "unresolved", "skipped", "error"}
        if self.status not in allowed:
            raise ValueError(f"invalid grade status: {self.status}")
        if self.status == "resolved" and self.resolved is not True:
            raise ValueError("resolved grade must set resolved=True")
        if self.status == "unresolved" and self.resolved is not False:
            raise ValueError("unresolved grade must set resolved=False")
        if self.status in {"skipped", "error"} and self.resolved is not None:
            raise ValueError(f"{self.status} grade must set resolved=None")


@dataclass(frozen=True)
class EvalResult:
    """Durable record for one benchmark, task, agent, and model tuple."""

    run_id: str
    benchmark: str
    instance_id: str
    agent: str
    started_at: str
    duration_ms: int
    task: dict[str, Any]
    agent_run: AgentRunResult | None
    grade: GradeResult | None
    error: str | None = None
    schema_version: int = EVAL_SCHEMA_VERSION

    @property
    def status(self) -> str:
        if self.error or self.grade is None:
            return "error"
        return self.grade.status


class BenchmarkAdapter(Protocol):
    """Load, prepare, and grade one family of benchmark tasks."""

    name: str

    def load_task(self, instance_id: str) -> EvalTask:
        """Return only information that is safe for the agent to observe."""

        ...

    def prepare(self, task: EvalTask, run_dir: Path) -> PreparedTask:
        """Create or select an isolated candidate workspace."""

        ...

    def grade(
        self,
        prepared: PreparedTask,
        agent_run: AgentRunResult,
        run_dir: Path,
        run_id: str,
    ) -> GradeResult:
        """Grade the patch outside the agent's workspace contract."""

        ...


class AgentAdapter(Protocol):
    """Run a coding agent behind a benchmark-neutral interface."""

    name: str

    def run(
        self,
        prepared: PreparedTask,
        budget: RunBudget,
        run_dir: Path,
    ) -> AgentRunResult:
        """Attempt the task and return a patch plus comparable metadata."""

        ...
