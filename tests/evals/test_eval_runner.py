from __future__ import annotations

import json
from pathlib import Path

from yada.evals import (
    AgentRunResult,
    EvalRunner,
    EvalTask,
    GradeResult,
    PreparedTask,
    RunBudget,
)


class FakeBenchmark:
    name = "fake"

    def load_task(self, instance_id: str) -> EvalTask:
        return EvalTask(instance_id, "Fix the bug", {"repo": "example/repo"})

    def prepare(self, task: EvalTask, run_dir: Path) -> PreparedTask:
        workspace = run_dir / "workspace"
        workspace.mkdir()
        return PreparedTask(task, workspace)

    def grade(self, prepared, agent_run, run_dir, run_id) -> GradeResult:
        assert agent_run.patch == "diff"
        return GradeResult("resolved", True, 7, tests_passed=2, tests_failed=0)


class FakeAgent:
    name = "fake-agent"

    def run(self, prepared, budget, run_dir) -> AgentRunResult:
        assert budget.max_steps == 4
        return AgentRunResult(
            agent=self.name,
            model="fake-model",
            status="completed",
            patch="diff",
            duration_ms=5,
            steps=2,
        )


def test_runner_composes_adapters_and_persists_result(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    runner = EvalRunner(
        benchmark=FakeBenchmark(),
        agent=FakeAgent(),
        budget=RunBudget(max_steps=4),
        output_path=output,
        artifact_dir=tmp_path / "artifacts",
        run_id="run-test",
    )

    result = runner.run("example__repo-1")

    assert result.status == "resolved"
    assert result.grade and result.grade.tests_passed == 2
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert stored["status"] == "resolved"
    assert stored["run_id"] == "run-test"
    assert stored["task"]["problem_statement_chars"] == len("Fix the bug")


def test_runner_persists_adapter_errors(tmp_path: Path) -> None:
    class BrokenBenchmark(FakeBenchmark):
        def prepare(self, task: EvalTask, run_dir: Path) -> PreparedTask:
            raise RuntimeError("container unavailable")

    output = tmp_path / "error.json"
    runner = EvalRunner(
        benchmark=BrokenBenchmark(),
        agent=FakeAgent(),
        budget=RunBudget(),
        output_path=output,
        artifact_dir=tmp_path / "artifacts",
        run_id="run-error",
    )

    result = runner.run("example__repo-2")

    assert result.status == "error"
    assert result.error == "RuntimeError: container unavailable"
    assert json.loads(output.read_text())["status"] == "error"
