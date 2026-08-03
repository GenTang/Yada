from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from yada.evals import AgentRunResult, EvalTask
from yada.evals.benchmarks import SWEbenchBenchmark
from yada.evals.benchmarks.swebench import (
    _AGENT_IMAGE_PREFIX,
    _INSTANCE_PREFIX,
    _load_harness_instance,
    _prepare_agent_image,
    _require_docker,
    _run_streaming,
)


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
    messages: list[str] = []
    benchmark = SWEbenchBenchmark(
        source_workspace=source,
        grade_mode="docker",
        harness_python="python-for-swebench",
        emit=messages.append,
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
        kwargs["stdout_path"].write_text("ok\n", encoding="utf-8")
        kwargs["stderr_path"].write_text("", encoding="utf-8")
        assert kwargs["label"] == "swebench grading"
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._run_streaming",
        fake_run,
    )

    grade = benchmark.grade(prepared, run, artifacts, "run-2")

    assert grade.status == "resolved"
    assert grade.resolved is True
    assert grade.details["official_report"]["schema_version"] == 2
    assert (artifacts / "swebench.stdout.log").read_text() == "ok\n"
    assert any("Official grading logs" in message for message in messages)


def test_swebench_requires_docker_before_official_work(monkeypatch) -> None:
    monkeypatch.setattr("yada.evals.benchmarks.swebench.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="--case PATH"):
        _require_docker()


def test_official_load_checks_docker_before_loading_dataset(monkeypatch) -> None:
    calls: list[str] = []

    def missing_docker():
        calls.append("docker")
        raise RuntimeError("Docker unavailable")

    def load_dataset(*_):
        calls.append("dataset")
        raise AssertionError("dataset must not load without Docker")

    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._require_docker", missing_docker
    )
    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._load_harness_instance", load_dataset
    )
    benchmark = SWEbenchBenchmark(harness_python="python-for-swebench")

    with pytest.raises(RuntimeError, match="Docker unavailable"):
        benchmark.load_task("owner__repo-1")

    assert calls == ["docker"]


def test_swebench_agent_image_metadata_comes_from_harness(monkeypatch) -> None:
    payload = {
        "image": "swebench/sweb.eval.x86_64.owner_repo-1:latest",
        "platform": "linux/x86_64",
        "workdir": "/testbed",
    }

    def fake_run(argv, **kwargs):
        assert argv[0] == "python-for-swebench"
        assert argv[1] == "-u"
        assert argv[-4:] == [
            "princeton-nlp/SWE-bench_Verified",
            "test",
            "owner__repo-1",
            "swebench",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            "build output\n" + _AGENT_IMAGE_PREFIX + json.dumps(payload) + "\n",
            "",
        )

    monkeypatch.setattr("yada.evals.benchmarks.swebench._run_streaming", fake_run)

    image, stdout, stderr = _prepare_agent_image(
        "python-for-swebench",
        "princeton-nlp/SWE-bench_Verified",
        "test",
        "owner__repo-1",
        "swebench",
    )

    assert image == payload
    assert "build output" in stdout
    assert stderr == ""


def test_streaming_process_tees_live_output_and_heartbeat(tmp_path: Path) -> None:
    stdout_path = tmp_path / "image.stdout.log"
    stderr_path = tmp_path / "image.stderr.log"
    messages: list[str] = []

    process = _run_streaming(
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time; "
                "print('building base image'); "
                "print('download warning', file=sys.stderr); "
                "time.sleep(0.1); "
                "print('building instance image')"
            ),
        ],
        timeout=5,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        emit=messages.append,
        heartbeat_seconds=0.02,
    )

    assert process.returncode == 0
    assert process.stdout == "building base image\nbuilding instance image\n"
    assert process.stderr == "download warning\n"
    assert "building base image" in stdout_path.read_text()
    assert "subprocess exited with status 0" in stdout_path.read_text()
    assert stderr_path.read_text() == "download warning\n"
    assert any("still running" in message for message in messages)
    assert any("building instance image" in message for message in messages)


def test_agent_image_failure_keeps_logs_and_reports_their_paths(
    tmp_path: Path, monkeypatch
) -> None:
    stdout_path = tmp_path / "image.stdout.log"
    stderr_path = tmp_path / "image.stderr.log"

    def fake_run(argv, **kwargs):
        kwargs["stdout_path"].write_text("partial build output\n", encoding="utf-8")
        kwargs["stderr_path"].write_text("build failed\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 1, "partial build output\n", "build failed\n"
        )

    monkeypatch.setattr("yada.evals.benchmarks.swebench._run_streaming", fake_run)

    with pytest.raises(RuntimeError) as error:
        _prepare_agent_image(
            "python-for-swebench",
            "princeton-nlp/SWE-bench_Verified",
            "test",
            "owner__repo-1",
            "swebench",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    assert "build failed" in str(error.value)
    assert str(stdout_path) in str(error.value)
    assert str(stderr_path) in str(error.value)
    assert stdout_path.read_text() == "partial build output\n"
    assert stderr_path.read_text() == "build failed\n"


def test_swebench_prepares_container_workspace_for_agent(
    tmp_path: Path, monkeypatch
) -> None:
    source, head = _source_repo(tmp_path)
    image = {
        "image": "swebench/sweb.eval.x86_64.owner_repo-1:latest",
        "platform": "linux/x86_64",
        "workdir": "/testbed",
    }

    def fake_prepare(*_, stdout_path, stderr_path, **__):
        stdout_path.write_text("prepared", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return image, "prepared", ""

    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._prepare_agent_image", fake_prepare
    )

    def fake_export(_image, workspace):
        shutil.copytree(source, workspace)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", "SWE-bench setup"],
            cwd=workspace,
            check=True,
        )

    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._export_image_workspace", fake_export
    )
    messages: list[str] = []
    benchmark = SWEbenchBenchmark(
        harness_python="python-for-swebench",
        emit=messages.append,
    )
    task = EvalTask(
        "owner__repo-1",
        "Fix VALUE.",
        {"repo": "owner/repo", "base_commit": head},
    )

    prepared = benchmark.prepare(task, tmp_path / "artifacts")

    assert prepared.metadata["command_backend"] == {
        "type": "docker",
        "image": image["image"],
        "platform": image["platform"],
        "workdir": "/testbed",
    }
    assert (prepared.workspace / "module.py").read_text() == "VALUE = 1\n"
    assert (tmp_path / "artifacts/swebench-agent-image.stdout.log").read_text() == (
        "prepared"
    )
    assert any("first run may take several minutes" in message for message in messages)
    assert any("Image preparation logs" in message for message in messages)
