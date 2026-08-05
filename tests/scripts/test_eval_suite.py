from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/eval_suite.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("yada_eval_suite", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
eval_suite = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = eval_suite
SCRIPT_SPEC.loader.exec_module(eval_suite)


CANARY_INSTANCES = [
    "pytest-dev__pytest-10051",
    "pytest-dev__pytest-10081",
    "pytest-dev__pytest-10356",
    "django__django-15987",
    "sympy__sympy-19637",
    "sphinx-doc__sphinx-9367",
    "scikit-learn__scikit-learn-13439",
    "pydata__xarray-6461",
]


def test_canary_manifest_is_versioned_and_contains_the_canary_eight() -> None:
    manifest = eval_suite.load_manifest(
        Path("benchmarks/suites/swebench-verified-canary-v1.json")
    )

    assert manifest.suite_id == "swebench-verified-canary-v1"
    assert manifest.benchmark == "swebench-verified"
    assert list(manifest.instances) == CANARY_INSTANCES
    assert manifest.sha256.startswith("sha256:")


def test_semantic_patch_id_ignores_object_ids_and_hunk_coordinates() -> None:
    first = """diff --git a/value.py b/value.py
index 1111111..2222222 100644
--- a/value.py
+++ b/value.py
@@ -1,2 +1,2 @@ function
-VALUE = 1
+VALUE = 2
 context
"""
    second = """diff --git a/value.py b/value.py
index aaaaaaa..bbbbbbb 100644
--- a/value.py
+++ b/value.py
@@ -20,2 +25,2 @@ function
-VALUE = 1
+VALUE = 2
 context
"""
    changed = second.replace("+VALUE = 2", "+VALUE = 3")

    assert eval_suite.semantic_patch_id(first) == eval_suite.semantic_patch_id(second)
    assert eval_suite.semantic_patch_id(first) != eval_suite.semantic_patch_id(changed)


def test_runner_continues_summarizes_and_skips_completed_attempts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = _write_manifest(tmp_path, ["owner__one-1", "owner__two-2"])
    suite_dir = tmp_path / "suite-results"
    secret = "sk-never-persist-this-value"
    key_file = tmp_path / "deepseek_api_key"
    key_file.write_text(secret + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr(eval_suite, "git_head", lambda _: "abc123")
    calls: list[list[str]] = []
    outcomes = ["resolved", "resolved", "unresolved", "missing"]

    def fake_run(command, *, cwd, check):
        assert command[1:5] == ["-m", "yada", "eval", "--swebench"]
        assert "--yes" in command
        assert secret not in command
        assert command[command.index("--api-key-file") + 1] == str(key_file)
        calls.append(command)
        outcome = outcomes[len(calls) - 1]
        if outcome != "missing":
            patch_variant = 1 if len(calls) == 1 else 20
            _write_result(
                command,
                status=outcome,
                steps=len(calls),
                tokens=100 * len(calls),
                patch=_patch(patch_variant, value=2 if len(calls) < 3 else 3),
            )
        return subprocess.CompletedProcess(
            command,
            0 if outcome == "resolved" else 1 if outcome == "unresolved" else 2,
        )

    monkeypatch.setattr(eval_suite.subprocess, "run", fake_run)

    assert (
        eval_suite.main(
            [
                "run",
                str(manifest),
                "--output-dir",
                str(suite_dir),
                "--repeat",
                "2",
                "--model",
                "test-model",
                "--api-key-file",
                str(key_file),
                "--max-steps",
                "7",
                "--wall-time",
                "90",
                "--max-output-tokens",
                "2048",
                "--python",
                "test-python",
            ]
        )
        == 0
    )

    summary_path = suite_dir / "summary.json"
    markdown_path = suite_dir / "summary.md"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["counts"] == {"error": 1, "resolved": 2, "unresolved": 1}
    assert summary["resolution_rate"] == 0.5
    assert summary["metrics"]["steps"] == {"max": 3, "median": 2, "min": 1}
    assert summary["metrics"]["tokens"] == {
        "max": 300,
        "median": 200,
        "min": 100,
    }
    first_instance, second_instance = summary["instances"]
    assert first_instance["patches_converged"] is True
    assert first_instance["metrics"]["steps"] == {
        "max": 2,
        "median": 1.5,
        "min": 1,
    }
    assert second_instance["patches_converged"] is False
    assert all(
        attempt["result_path"] and not Path(attempt["result_path"]).is_absolute()
        for attempt in first_instance["attempts"]
    )
    assert "Development canary only" in markdown_path.read_text(encoding="utf-8")
    assert secret not in "".join(
        path.read_text(encoding="utf-8") for path in suite_dir.rglob("*.json")
    )

    json_before = summary_path.read_bytes()
    markdown_before = markdown_path.read_bytes()
    assert eval_suite.main(["run", str(manifest), "--resume", str(suite_dir)]) == 0
    assert len(calls) == 4
    assert summary_path.read_bytes() == json_before
    assert markdown_path.read_bytes() == markdown_before


def test_resume_recovers_a_result_written_just_before_interruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = _write_manifest(tmp_path, ["owner__repo-1"])
    suite_dir = tmp_path / "suite-results"
    monkeypatch.setattr(eval_suite, "git_head", lambda _: "abc123")
    calls = 0

    def interrupted_run(command, *, cwd, check):
        nonlocal calls
        calls += 1
        _write_result(
            command,
            status="resolved",
            steps=4,
            tokens=123,
            patch=_patch(1, value=2),
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(eval_suite.subprocess, "run", interrupted_run)

    assert (
        eval_suite.main(["run", str(manifest), "--output-dir", str(suite_dir)]) == 130
    )
    assert not list(suite_dir.rglob("attempt.json"))

    def must_not_run(*args, **kwargs):
        raise AssertionError("a completed result must be recovered, not rerun")

    monkeypatch.setattr(eval_suite.subprocess, "run", must_not_run)

    assert eval_suite.main(["run", str(manifest), "--resume", str(suite_dir)]) == 0
    assert calls == 1
    markers = list(suite_dir.rglob("attempt.json"))
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker["status"] == "resolved"
    assert marker["recovered"] is True
    assert marker["return_code"] is None


def test_resume_rejects_a_changed_manifest(tmp_path: Path, monkeypatch) -> None:
    manifest = _write_manifest(tmp_path, ["owner__repo-1"])
    suite_dir = tmp_path / "suite-results"
    monkeypatch.setattr(eval_suite, "git_head", lambda _: "abc123")

    def fake_run(command, *, cwd, check):
        _write_result(
            command,
            status="resolved",
            steps=1,
            tokens=10,
            patch="",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(eval_suite.subprocess, "run", fake_run)
    assert eval_suite.main(["run", str(manifest), "--output-dir", str(suite_dir)]) == 0

    manifest.write_text(manifest.read_text() + "\n", encoding="utf-8")

    assert eval_suite.main(["run", str(manifest), "--resume", str(suite_dir)]) == 2


def _write_manifest(tmp_path: Path, instances: list[str]) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": "test-suite-v1",
                "benchmark": "swebench-verified",
                "instances": instances,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_result(
    command: list[str],
    *,
    status: str,
    steps: int,
    tokens: int,
    patch: str,
) -> None:
    result_path = Path(command[command.index("--output") + 1])
    artifacts = Path(command[command.index("--artifact-dir") + 1])
    artifacts.mkdir(parents=True, exist_ok=True)
    trace = artifacts / "yada-trace.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    instance_id = command[command.index("--swebench") + 1]
    result_path.write_text(
        json.dumps(
            {
                "status": status,
                "instance_id": instance_id,
                "started_at": "2026-01-01T00:00:00+00:00",
                "duration_ms": 500,
                "error": "synthetic failure" if status == "error" else None,
                "agent_run": {
                    "model": "test-model",
                    "steps": steps,
                    "usage": {"total_tokens": tokens},
                    "duration_ms": steps * 1_000,
                    "patch": patch,
                    "trace_path": str(trace),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _patch(line: int, *, value: int) -> str:
    return f"""diff --git a/value.py b/value.py
index 1111111..2222222 100644
--- a/value.py
+++ b/value.py
@@ -{line} +{line} @@
-VALUE = 1
+VALUE = {value}
"""
