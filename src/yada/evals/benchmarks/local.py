"""Manifest-driven adapter for small local coding benchmarks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from yada.evals.base import AgentRunResult, EvalTask, GradeResult, PreparedTask
from yada.evals.benchmarks.local_environment import (
    prepare_environment,
    workspace_environment,
)
from yada.evals.benchmarks.local_source import (
    prepare_source,
    require_clean,
    require_head,
)


class LocalBenchmark:
    """Load a public task and external grader from a JSON manifest."""

    name = "local"

    def __init__(
        self,
        manifest_path: Path,
        *,
        cache_root: Path | None = None,
    ) -> None:
        self.manifest_path = manifest_path.expanduser().resolve()
        self.base_dir = self.manifest_path.parent
        self.cache_root = (
            cache_root.expanduser().resolve()
            if cache_root is not None
            else (Path.cwd() / ".yada" / "cache" / "evals").resolve()
        )
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
        instance_file_value = self.manifest.get("instance_file")
        supplied = sum(
            bool(value)
            for value in (task_file_value, statement_value, instance_file_value)
        )
        if supplied != 1:
            raise ValueError(
                "manifest must provide exactly one of task_file, instance_file, "
                "or problem_statement"
            )
        if task_file_value:
            task_file = self._path(_required_string(self.manifest, "task_file"))
            try:
                problem_statement = task_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"cannot read task file: {exc}") from exc
            public_metadata: dict[str, Any] = {}
        elif instance_file_value:
            instance_file = self._path(_required_string(self.manifest, "instance_file"))
            row = _load_instance_file(instance_file)
            row_id = _required_string(row, "instance_id")
            if row_id != manifest_id:
                raise ValueError(
                    f"instance file contains {row_id!r}, not {manifest_id!r}"
                )
            problem_statement = _required_string(row, "problem_statement")
            public_metadata = {
                key: row.get(key)
                for key in (
                    "repo",
                    "base_commit",
                    "version",
                    "difficulty",
                    "dataset_name",
                    "dataset_revision",
                    "created_at",
                    "environment_setup_commit",
                    "split",
                )
                if row.get(key) is not None
            }
        else:
            problem_statement = str(statement_value)
            public_metadata = {}

        metadata = {
            "manifest": str(self.manifest_path),
            "manifest_sha256": _sha256(self.manifest_path),
            "base_commit": self.manifest.get("base_commit"),
            **public_metadata,
        }
        if instance_file_value:
            metadata["instance_sha256"] = _sha256(instance_file)
        environment = self.manifest.get("environment")
        if isinstance(environment, dict) and environment.get("type") == "uv":
            project = self._path(str(environment.get("project", ".")))
            lockfile = project / "uv.lock"
            if lockfile.is_file():
                metadata["environment_lock_sha256"] = _sha256(lockfile)
        return EvalTask(manifest_id, problem_statement, metadata)

    def prepare(self, task: EvalTask, run_dir: Path) -> PreparedTask:
        run_dir.mkdir(parents=True, exist_ok=True)
        source = prepare_source(
            self.manifest.get("workspace"),
            base_dir=self.base_dir,
            cache_root=self.cache_root,
            base_commit=self.manifest.get("base_commit"),
        )
        if not source.is_dir():
            raise ValueError(f"local benchmark workspace does not exist: {source}")
        command_environment = prepare_environment(
            self.manifest.get("environment"),
            base_dir=self.base_dir,
            source=source,
        )
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
                require_clean(source)
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
            require_head(workspace, str(expected_commit))
        command_environment.update(
            workspace_environment(self.manifest.get("environment"), workspace)
        )
        return PreparedTask(
            task,
            workspace,
            {
                "source_workspace": str(source),
                "environment": command_environment,
            },
        )

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
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else self.base_dir / raw
        # Keep the final symlink intact: resolving ``.venv/bin/python`` to the
        # base interpreter would discard the virtual environment at execution.
        candidate = Path(os.path.abspath(candidate))
        return str(candidate) if candidate.exists() else value


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest {key} must be a non-empty string")
    return value


def _load_instance_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read local instance file: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("local instance file must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
