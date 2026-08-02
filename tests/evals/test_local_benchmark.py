from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from yada.evals import EvalRunner, RunBudget
from yada.evals.agents import CommandAgentAdapter
from yada.evals.benchmarks import LocalBenchmark


def test_local_manifest_runs_agent_in_copy_and_external_grader(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 'broken'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
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
