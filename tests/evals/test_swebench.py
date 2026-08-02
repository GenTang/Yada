from __future__ import annotations

import json
import subprocess
from pathlib import Path

from yada.evals import AgentRunResult
from yada.evals.benchmarks import SWEbenchBenchmark


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "add", "module.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return source, head


def _instance_file(tmp_path: Path, head: str) -> Path:
    path = tmp_path / "instance.json"
    path.write_text(
        json.dumps(
            {
                "instance_id": "owner__repo-1",
                "repo": "owner/repo",
                "base_commit": head,
                "problem_statement": "Fix VALUE.",
                "patch": "SECRET GOLD PATCH",
                "test_patch": "SECRET TEST PATCH",
                "FAIL_TO_PASS": "hidden test",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_swebench_task_hides_gold_data_and_writes_prediction(tmp_path: Path) -> None:
    source, head = _source_repo(tmp_path)
    benchmark = SWEbenchBenchmark(
        instance_file=_instance_file(tmp_path, head),
        source_workspace=source,
        grade_mode="none",
    )

    task = benchmark.load_task("owner__repo-1")
    prepared = benchmark.prepare(task, tmp_path / "artifacts")
    run = AgentRunResult(
        agent="test-agent",
        model="test-model",
        status="completed",
        patch="diff --git a/module.py b/module.py\n",
        duration_ms=1,
    )
    grade = benchmark.grade(prepared, run, tmp_path / "artifacts", "run-1")

    assert "patch" not in task.metadata
    assert "test_patch" not in task.metadata
    assert "FAIL_TO_PASS" not in task.metadata
    assert grade.status == "skipped"
    prediction = json.loads(
        (tmp_path / "artifacts/predictions.jsonl").read_text()
    )
    assert prediction == {
        "instance_id": "owner__repo-1",
        "model_name_or_path": "test-agent/test-model",
        "model_patch": "diff --git a/module.py b/module.py\n",
    }


def test_swebench_parses_official_run_report(tmp_path: Path, monkeypatch) -> None:
    source, head = _source_repo(tmp_path)
    benchmark = SWEbenchBenchmark(
        instance_file=_instance_file(tmp_path, head),
        source_workspace=source,
        grade_mode="docker",
        harness_python="python-for-swebench",
    )
    artifacts = tmp_path / "artifacts"
    task = benchmark.load_task("owner__repo-1")
    prepared = benchmark.prepare(task, artifacts)
    run = AgentRunResult(
        agent="yada",
        model="deepseek-v4-pro",
        status="completed",
        patch="a patch",
        duration_ms=1,
    )

    def fake_run(argv, **kwargs):
        report = kwargs["cwd"] / "yada__deepseek-v4-pro.run-2.json"
        report.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "resolved_ids": ["owner__repo-1"],
                    "unresolved_ids": [],
                    "error_ids": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench.subprocess.run",
        fake_run,
    )

    grade = benchmark.grade(prepared, run, artifacts, "run-2")

    assert grade.status == "resolved"
    assert grade.resolved is True
    assert grade.details["official_report"]["schema_version"] == 2
