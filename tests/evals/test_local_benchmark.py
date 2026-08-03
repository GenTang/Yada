from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from yada.evals import EvalRunner, RunBudget
from yada.evals.agents import CommandAgentAdapter
from yada.evals.benchmarks import LocalBenchmark, SWEbenchBenchmark


def test_local_manifest_runs_agent_in_copy_and_external_grader(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 'broken'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=source, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    task_file = tmp_path / "task.md"
    task_file.write_text("Change broken to fixed.", encoding="utf-8")
    grader = tmp_path / "grader.py"
    grader.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "text = (Path(sys.argv[1]) / 'app.py').read_text()\n"
        "print('1 passed' if \"'fixed'\" in text else '1 failed')\n"
        "raise SystemExit(0 if \"'fixed'\" in text else 1)\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "local-fix",
                "task_file": "task.md",
                "workspace": "source",
                "workspace_mode": "copy",
                "base_commit": head,
                "grader": {"argv": [sys.executable, "grader.py", "{workspace}"]},
            }
        ),
        encoding="utf-8",
    )
    agent = CommandAgentAdapter(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('app.py').write_text(\"value = 'fixed'\\n\")",
        ],
        name="fixture-agent",
        model="fixture-model",
    )
    output = tmp_path / "result.json"
    runner = EvalRunner(
        benchmark=LocalBenchmark(manifest),
        agent=agent,
        budget=RunBudget(),
        output_path=output,
        artifact_dir=tmp_path / "artifacts",
        run_id="local-run",
    )

    result = runner.run("")

    assert result.status == "resolved"
    assert result.grade and result.grade.tests_passed == 1
    assert "broken" in (source / "app.py").read_text()
    assert "fixed" in (tmp_path / "artifacts/workspace/app.py").read_text()
    assert "value = 'fixed'" in (result.agent_run.patch if result.agent_run else "")


def test_local_manifest_can_load_shared_public_instance(tmp_path: Path) -> None:
    instance = tmp_path / "instance.json"
    instance.write_text(
        json.dumps(
            {
                "instance_id": "owner__repo-1",
                "repo": "owner/repo",
                "base_commit": "abc123",
                "problem_statement": "Fix the public bug.",
                "patch": "SECRET GOLD PATCH",
                "test_patch": "SECRET TEST PATCH",
                "FAIL_TO_PASS": ["secret test"],
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "case.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "owner__repo-1",
                "instance_file": "instance.json",
                "workspace": "unused",
            }
        ),
        encoding="utf-8",
    )

    task = LocalBenchmark(manifest).load_task("")

    assert task.problem_statement == "Fix the public bug."
    assert task.metadata["repo"] == "owner/repo"
    assert "patch" not in task.metadata
    assert "test_patch" not in task.metadata
    assert "FAIL_TO_PASS" not in task.metadata


def test_local_manifest_bootstraps_exact_git_commit_into_cache(tmp_path: Path) -> None:
    source = tmp_path / "upstream"
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
    manifest = tmp_path / "case.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "owner__repo-2",
                "problem_statement": "Change VALUE.",
                "workspace": {
                    "type": "git",
                    "url": str(source),
                    "cache_key": "owner/repo-2",
                },
                "base_commit": head,
            }
        ),
        encoding="utf-8",
    )
    benchmark = LocalBenchmark(manifest, cache_root=tmp_path / "cache")
    task = benchmark.load_task("")

    prepared = benchmark.prepare(task, tmp_path / "artifacts")

    cached = tmp_path / "cache/owner/repo-2/repo"
    assert cached.is_dir()
    assert prepared.workspace != cached
    assert (prepared.workspace / "module.py").read_text() == "VALUE = 1\n"


def test_local_grader_keeps_virtualenv_python_symlink(tmp_path: Path) -> None:
    environment_bin = tmp_path / ".venv/bin"
    environment_bin.mkdir(parents=True)
    python_link = environment_bin / "python"
    python_link.symlink_to(sys.executable)
    manifest = tmp_path / "case.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "local-symlink",
                "problem_statement": "Keep the environment selected.",
                "workspace": "unused",
            }
        ),
        encoding="utf-8",
    )

    resolved = LocalBenchmark(manifest)._resolve_argv_item(".venv/bin/python")

    assert resolved == str(python_link)


def test_checked_in_case_shares_prompt_with_swebench_adapter(monkeypatch) -> None:
    case_dir = (
        Path(__file__).resolve().parents[2]
        / "benchmarks/swebench_verified/pytest-10051"
    )
    local_task = LocalBenchmark(case_dir / "case.json").load_task("")
    instance = json.loads((case_dir / "instance.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._load_harness_instance",
        lambda *_: instance,
    )
    monkeypatch.setattr(
        "yada.evals.benchmarks.swebench._require_docker",
        lambda: None,
    )
    swebench_task = SWEbenchBenchmark(
        grade_mode="none",
    ).load_task("pytest-dev__pytest-10051")

    assert local_task.problem_statement == swebench_task.problem_statement
    assert local_task.metadata["base_commit"] == swebench_task.metadata["base_commit"]
