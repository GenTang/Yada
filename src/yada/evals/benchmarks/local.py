"""Manifest-driven adapter for small local coding benchmarks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from yada.evals.base import AgentRunResult, EvalTask, GradeResult, PreparedTask


class LocalBenchmark:
    """Load a public task and external grader from a JSON manifest."""

    name = "local"

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path.expanduser().resolve()
        self.base_dir = self.manifest_path.parent
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read local benchmark manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("local benchmark manifest must be a JSON object")
        if manifest.get("schema_version") != 1:
            raise ValueError("local benchmark manifest schema_version must be 1")
        self.manifest = manifest

    def load_task(self, instance_id: str) -> EvalTask:
        manifest_id = _required_string(self.manifest, "instance_id")
        if instance_id and instance_id != manifest_id:
            raise ValueError(
                f"manifest contains {manifest_id!r}, not requested {instance_id!r}"
            )

        task_file_value = self.manifest.get("task_file")
        statement_value = self.manifest.get("problem_statement")
        if bool(task_file_value) == bool(statement_value):
            raise ValueError(
                "manifest must provide exactly one of task_file or problem_statement"
            )
        if task_file_value:
            task_file = self._path(_required_string(self.manifest, "task_file"))
            try:
                problem_statement = task_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"cannot read task file: {exc}") from exc
        else:
            problem_statement = str(statement_value)

        metadata = {
            "manifest": str(self.manifest_path),
            "base_commit": self.manifest.get("base_commit"),
        }
        return EvalTask(manifest_id, problem_statement, metadata)

    def prepare(self, task: EvalTask, run_dir: Path) -> PreparedTask:
        run_dir.mkdir(parents=True, exist_ok=True)
        source = self._path(_required_string(self.manifest, "workspace"))
        if not source.is_dir():
            raise ValueError(f"local benchmark workspace does not exist: {source}")
        mode = self.manifest.get("workspace_mode", "copy")
        if mode not in {"copy", "in_place"}:
            raise ValueError("workspace_mode must be 'copy' or 'in_place'")

        if mode == "in_place":
            workspace = source
        else:
            workspace = run_dir / "workspace"
            if workspace.exists():
                raise ValueError(f"prepared workspace already exists: {workspace}")
            if (source / ".git").exists():
                _require_clean(source)
            # Local fixtures may contain generated, Git-ignored files created by
            # their pinned environment. Preserve those while isolating mutations.
            shutil.copytree(
                source,
                workspace,
                symlinks=True,
                ignore=shutil.ignore_patterns(".yada", "__pycache__", ".pytest_cache"),
            )

        expected_commit = self.manifest.get("base_commit")
        if expected_commit:
            _require_head(workspace, str(expected_commit))
        return PreparedTask(task, workspace, {"source_workspace": str(source)})

    def grade(
        self,
        prepared: PreparedTask,
        agent_run: AgentRunResult,
        run_dir: Path,
        run_id: str,
    ) -> GradeResult:
        grader = self.manifest.get("grader")
        if grader is None:
            return GradeResult(
                status="skipped",
                resolved=None,
                duration_ms=0,
                details={"reason": "manifest has no grader"},
            )
        if not isinstance(grader, dict) or not isinstance(grader.get("argv"), list):
            raise ValueError("manifest grader.argv must be an array")

        patch_path = run_dir / "agent.patch"
        patch_path.write_text(agent_run.patch, encoding="utf-8")
        substitutions = {
            "workspace": str(prepared.workspace),
            "patch": str(patch_path),
            "run_dir": str(run_dir),
            "run_id": run_id,
        }
        argv = [
            _substitute(self._resolve_argv_item(str(item)), substitutions)
            for item in grader["argv"]
        ]
        if not argv or not all(argv):
            raise ValueError("manifest grader.argv must contain non-empty strings")

        started = time.monotonic()
        timeout = int(grader.get("timeout_seconds", 1_800))
        try:
            process = subprocess.run(
                argv,
                cwd=self.base_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            return_code = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as exc:
            return_code = 124
            stdout = _timeout_text(exc.stdout)
            stderr = _timeout_text(exc.stderr)

        stdout_path = run_dir / "grader.stdout.log"
        stderr_path = run_dir / "grader.stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        passed, failed = _pytest_counts(stdout + "\n" + stderr)
        if return_code == 0:
            status = "resolved"
            resolved: bool | None = True
        elif return_code == 1:
            status = "unresolved"
            resolved = False
        else:
            status = "error"
            resolved = None
        return GradeResult(
            status=status,
            resolved=resolved,
            duration_ms=round((time.monotonic() - started) * 1000),
            tests_passed=passed,
            tests_failed=failed,
            details={
                "argv": argv,
                "return_code": return_code,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            },
        )

    def _path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.base_dir / path).resolve()

    def _resolve_argv_item(self, value: str) -> str:
        if "{" in value or not ("/" in value or value.startswith(".")):
            return value
        candidate = self._path(value)
        return str(candidate) if candidate.exists() else value


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest {key} must be a non-empty string")
    return value


def _require_head(workspace: Path, expected: str) -> None:
    result = _run(["git", "rev-parse", "HEAD"], workspace)
    actual = result.stdout.strip()
    if result.returncode or actual != expected:
        raise ValueError(f"expected base commit {expected}, got {actual or 'unknown'}")


def _require_clean(workspace: Path) -> None:
    result = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        workspace,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "cannot inspect workspace status")
    if result.stdout.strip():
        raise ValueError("local source workspace must be clean before copy mode")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )


def _substitute(value: str, substitutions: dict[str, str]) -> str:
    for key, replacement in substitutions.items():
        value = value.replace("{" + key + "}", replacement)
    return value


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _pytest_counts(output: str) -> tuple[int | None, int | None]:
    passed_matches = re.findall(r"(\d+) passed", output)
    failed_matches = re.findall(r"(\d+) failed", output)
    passed = int(passed_matches[-1]) if passed_matches else None
    failed = int(failed_matches[-1]) if failed_matches else None
    return passed, failed
