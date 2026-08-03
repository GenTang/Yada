from __future__ import annotations

import json
import subprocess
from pathlib import Path

from yada.evals import AgentRunResult
from yada.evals.benchmarks import SWEbenchBenchmark
from yada.evals.benchmarks.swebench import _INSTANCE_PREFIX, _load_harness_instance


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=source, check=True
    )
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


def _instance_row(head: str) -> dict[str, str]:
    return {
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "base_commit": head,
        "problem_statement": "Fix VALUE.",
    }


def test_swebench_metadata_comes_from_harness_and_stays_public(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        assert argv[0] == "python-for-swebench"
        assert argv[-3:] == [
            "princeton-nlp/SWE-bench_Verified",
            "test",
            "owner__repo-1",
        ]
        payload = {
            "instance_id": "owner__repo-1",
            "repo": "owner/repo",
            "base_commit": "abc123",
            "problem_statement": "Fix VALUE.",
            "patch": "SECRET GOLD PATCH",
        }
        return subprocess.CompletedProcess(
            argv,
            0,
            _INSTANCE_PREFIX + json.dumps(payload) + "\n",
            "",
        )

    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench.subprocess.run",
        fake_run,
    )

    row = _load_harness_instance(
        "python-for-swebench",
        "princeton-nlp/SWE-bench_Verified",
        "test",
        "owner__repo-1",
    )

    assert row["problem_statement"] == "Fix VALUE."
    assert "patch" not in row


def test_swebench_loads_task_with_harness_and_writes_prediction(
    tmp_path: Path, monkeypatch
) -> None:
    source, head = _source_repo(tmp_path)
    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._load_harness_instance",
        lambda *_: _instance_row(head),
    )
    benchmark = SWEbenchBenchmark(
        source_workspace=source,
        grade_mode="none",
        harness_python="python-for-swebench",
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
    prediction = json.loads((tmp_path / "artifacts/predictions.jsonl").read_text())
    assert prediction == {
        "instance_id": "owner__repo-1",
        "model_name_or_path": "test-agent/test-model",
        "model_patch": "diff --git a/module.py b/module.py\n",
    }


def test_swebench_parses_official_run_report(tmp_path: Path, monkeypatch) -> None:
    source, head = _source_repo(tmp_path)
    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._load_harness_instance",
        lambda *_: _instance_row(head),
    )
    benchmark = SWEbenchBenchmark(
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
