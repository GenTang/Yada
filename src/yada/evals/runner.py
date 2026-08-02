"""Benchmark-neutral execution pipeline."""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from yada.evals.base import (
    AgentAdapter,
    AgentRunResult,
    BenchmarkAdapter,
    EvalResult,
    EvalTask,
    GradeResult,
    RunBudget,
)
from yada.evals.result import write_result


class EvalRunner:
    """Compose one benchmark adapter with one agent adapter."""

    def __init__(
        self,
        *,
        benchmark: BenchmarkAdapter,
        agent: AgentAdapter,
        budget: RunBudget,
        output_path: Path,
        artifact_dir: Path,
        run_id: str | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.agent = agent
        self.budget = budget
        self.output_path = output_path.expanduser().resolve()
        self.artifact_dir = artifact_dir.expanduser().resolve()
        self.run_id = run_id or _new_run_id()

    def run(self, instance_id: str) -> EvalResult:
        """Run one task, always persisting a result even on ordinary errors."""

        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        task: EvalTask | None = None
        agent_run: AgentRunResult | None = None
        grade: GradeResult | None = None
        error: str | None = None

        try:
            task = self.benchmark.load_task(instance_id)
            prepared = self.benchmark.prepare(task, self.artifact_dir)
            agent_run = self.agent.run(prepared, self.budget, self.artifact_dir)
            grade = self.benchmark.grade(
                prepared,
                agent_run,
                self.artifact_dir,
                self.run_id,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        result = EvalResult(
            run_id=self.run_id,
            benchmark=self.benchmark.name,
            instance_id=task.instance_id if task else instance_id,
            agent=self.agent.name,
            started_at=started_at,
            duration_ms=round((time.monotonic() - started) * 1000),
            task=_task_record(task),
            agent_run=agent_run,
            grade=grade,
            error=error,
        )
        write_result(self.output_path, result)
        return result


def _task_record(task: EvalTask | None) -> dict[str, object]:
    if task is None:
        return {}
    statement = task.problem_statement.encode("utf-8")
    return {
        "instance_id": task.instance_id,
        "problem_statement_sha256": hashlib.sha256(statement).hexdigest(),
        "problem_statement_chars": len(task.problem_statement),
        "metadata": task.metadata,
    }


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"yada-{timestamp}-{uuid.uuid4().hex[:8]}"
