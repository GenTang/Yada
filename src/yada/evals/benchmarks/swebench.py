"""Adapter for official SWE-bench datasets and Docker evaluation harness."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from yada.evals.base import AgentRunResult, EvalTask, GradeResult, PreparedTask


class SWEbenchBenchmark:
    """Prepare a base repository and delegate grading to SWE-bench Harness."""

    name = "swebench"

    def __init__(
        self,
        *,
        dataset_name: str = "SWE-bench/SWE-bench_Verified",
        split: str = "test",
        instance_file: Path | None = None,
        source_workspace: Path | None = None,
        harness_python: str = sys.executable,
        grade_mode: str = "docker",
        cache_level: str = "env",
        clean: bool = False,
        namespace: str | None = "swebench",
        grade_timeout_seconds: int = 1_800,
    ) -> None:
        if grade_mode not in {"docker", "none"}:
            raise ValueError("grade_mode must be 'docker' or 'none'")
        if cache_level not in {"none", "base", "env", "instance"}:
            raise ValueError("invalid SWE-bench cache level")
        self.dataset_name = dataset_name
        self.split = split
        self.instance_file = (
            instance_file.expanduser().resolve() if instance_file else None
        )
        self.source_workspace = (
            source_workspace.expanduser().resolve() if source_workspace else None
        )
        self.harness_python = harness_python
        self.grade_mode = grade_mode
        self.cache_level = cache_level
        self.clean = clean
        self.namespace = namespace
        self.grade_timeout_seconds = grade_timeout_seconds

    def load_task(self, instance_id: str) -> EvalTask:
        row = (
            _load_instance_file(self.instance_file, instance_id)
            if self.instance_file
            else _load_huggingface_instance(
                self.dataset_name,
                self.split,
                instance_id,
            )
        )
        required = ("instance_id", "repo", "base_commit", "problem_statement")
        missing = [key for key in required if not row.get(key)]
        if missing:
            raise ValueError(f"SWE-bench instance is missing fields: {missing}")
        if row["instance_id"] != instance_id:
            raise ValueError(
                f"instance file contains {row['instance_id']!r}, not {instance_id!r}"
            )

        # Gold patches, hidden test patches, and test IDs deliberately do not cross
        # the public task boundary.
        metadata = {
            "repo": str(row["repo"]),
            "base_commit": str(row["base_commit"]),
            "version": row.get("version"),
            "difficulty": row.get("difficulty"),
            "dataset_name": self.dataset_name,
            "split": self.split,
        }
        return EvalTask(
            instance_id=str(row["instance_id"]),
            problem_statement=str(row["problem_statement"]),
            metadata=metadata,
        )

    def prepare(self, task: EvalTask, run_dir: Path) -> PreparedTask:
        run_dir.mkdir(parents=True, exist_ok=True)
        workspace = run_dir / "workspace"
        if workspace.exists():
            raise ValueError(f"prepared workspace already exists: {workspace}")
        repo = str(task.metadata["repo"])
        base_commit = str(task.metadata["base_commit"])

        if self.source_workspace is not None:
            _require_head(self.source_workspace, base_commit)
            result = _run(
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--quiet",
                    str(self.source_workspace),
                    str(workspace),
                ],
                run_dir,
            )
            if result.returncode:
                raise RuntimeError(
                    result.stderr.strip() or "failed to clone source workspace"
                )
        else:
            workspace.mkdir(parents=True)
            commands = [
                ["git", "init", "--quiet"],
                ["git", "remote", "add", "origin", f"https://github.com/{repo}.git"],
                ["git", "fetch", "--quiet", "--depth", "1", "origin", base_commit],
                ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            ]
            for argv in commands:
                result = _run(argv, workspace, timeout=600)
                if result.returncode:
                    raise RuntimeError(
                        result.stderr.strip() or f"failed command: {argv}"
                    )
        _require_head(workspace, base_commit)
        return PreparedTask(
            task,
            workspace,
            {"repo": repo, "base_commit": base_commit},
        )

    def grade(
        self,
        prepared: PreparedTask,
        agent_run: AgentRunResult,
        run_dir: Path,
        run_id: str,
    ) -> GradeResult:
        prediction_path = run_dir / "predictions.jsonl"
        model_name = _model_name(agent_run)
        prediction = {
            "instance_id": prepared.task.instance_id,
            "model_name_or_path": model_name,
            "model_patch": agent_run.patch,
        }
        prediction_path.write_text(
            json.dumps(prediction, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if self.grade_mode == "none":
            return GradeResult(
                status="skipped",
                resolved=None,
                duration_ms=0,
                details={
                    "reason": "SWE-bench Docker grading disabled",
                    "predictions_path": str(prediction_path),
                },
            )

        argv = [
            self.harness_python,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            self.dataset_name,
            "--split",
            self.split,
            "--predictions_path",
            str(prediction_path),
            "--instance_ids",
            prepared.task.instance_id,
            "--max_workers",
            "1",
            "--run_id",
            run_id,
            "--cache_level",
            self.cache_level,
            "--clean",
            str(self.clean).lower(),
            "--timeout",
            str(self.grade_timeout_seconds),
            "--namespace",
            self.namespace if self.namespace is not None else "none",
        ]
        started = time.monotonic()
        timed_out = False
        try:
            process = subprocess.run(
                argv,
                cwd=run_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.grade_timeout_seconds + 600,
                check=False,
            )
            return_code = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            return_code = 124
            stdout = _timeout_text(exc.stdout)
            stderr = _timeout_text(exc.stderr)
        stdout_path = run_dir / "swebench.stdout.log"
        stderr_path = run_dir / "swebench.stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        report_path = run_dir / f"{model_name.replace('/', '__')}.{run_id}.json"
        duration_ms = round((time.monotonic() - started) * 1000)

        details: dict[str, Any] = {
            "argv": argv,
            "return_code": return_code,
            "timed_out": timed_out,
            "predictions_path": str(prediction_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        if not report_path.is_file():
            details["reason"] = "SWE-bench Harness did not produce a run report"
            return GradeResult("error", None, duration_ms, details=details)

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            details["reason"] = f"cannot parse SWE-bench report: {exc}"
            return GradeResult("error", None, duration_ms, details=details)
        details["official_report"] = report
        instance_id = prepared.task.instance_id
        if instance_id in report.get("resolved_ids", []):
            return GradeResult("resolved", True, duration_ms, details=details)
        if instance_id in report.get("unresolved_ids", []):
            return GradeResult("unresolved", False, duration_ms, details=details)
        details["reason"] = "instance was not completed by SWE-bench Harness"
        return GradeResult("error", None, duration_ms, details=details)


def _load_instance_file(path: Path, instance_id: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read SWE-bench instance file: {exc}") from exc
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            rows: list[Any] = data.get("rows", [data])
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]

    for item in rows:
        row = item.get("row", item) if isinstance(item, dict) else {}
        if row.get("instance_id") == instance_id:
            return dict(row)
    raise ValueError(f"instance {instance_id!r} not found in {path}")


def _load_huggingface_instance(
    dataset_name: str,
    split: str,
    instance_id: str,
) -> dict[str, Any]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "loading SWE-bench from Hugging Face requires the optional "
            "'datasets' package; install it or pass --instance-file"
        ) from exc
    dataset = load_dataset(dataset_name, split=split)
    for row in dataset:
        if row.get("instance_id") == instance_id:
            return dict(row)
    raise ValueError(
        f"instance {instance_id!r} not found in {dataset_name} split {split}"
    )


def _require_head(workspace: Path, expected: str) -> None:
    result = _run(["git", "rev-parse", "HEAD"], workspace)
    actual = result.stdout.strip()
    if result.returncode or actual != expected:
        raise ValueError(f"expected base commit {expected}, got {actual or 'unknown'}")


def _run(
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _model_name(agent_run: AgentRunResult) -> str:
    raw = f"{agent_run.agent}/{agent_run.model}"
    return re.sub(r"[^A-Za-z0-9._/-]+", "-", raw).strip("-") or "unknown"


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
