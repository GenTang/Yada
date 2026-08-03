"""Adapter for official SWE-bench datasets and Docker evaluation harness."""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from yada.evals.base import AgentRunResult, EvalTask, GradeResult, PreparedTask

_PUBLIC_INSTANCE_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "version",
    "difficulty",
    "dataset_revision",
    "created_at",
    "environment_setup_commit",
)
_SWEBENCH_PACKAGE = "swebench==4.1.0"
_INSTANCE_PREFIX = "__YADA_SWEBENCH_INSTANCE__="
_AGENT_IMAGE_PREFIX = "__YADA_SWEBENCH_AGENT_IMAGE__="
_INSTANCE_LOADER = f"""
import json
import sys
from swebench.harness.utils import load_swebench_dataset

dataset_name, split, instance_id = sys.argv[1:4]
rows = load_swebench_dataset(dataset_name, split, [instance_id])
row = next((item for item in rows if item.get("instance_id") == instance_id), None)
if row is None:
    raise SystemExit(f"instance {{instance_id!r}} not found")
public_fields = {_PUBLIC_INSTANCE_FIELDS!r}
public_row = {{key: row.get(key) for key in public_fields}}
print({_INSTANCE_PREFIX!r} + json.dumps(public_row, ensure_ascii=False, default=str))
"""
_AGENT_IMAGE_PREPARER = f"""
import json
import sys
import docker
from swebench.harness.docker_build import build_instance_images
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.utils import load_swebench_dataset

dataset_name, split, instance_id, namespace_value = sys.argv[1:5]
namespace = None if namespace_value == "none" else namespace_value
rows = load_swebench_dataset(dataset_name, split, [instance_id])
row = next((item for item in rows if item.get("instance_id") == instance_id), None)
if row is None:
    raise SystemExit(f"instance {{instance_id!r}} not found")

client = docker.from_env()
client.ping()
spec = make_test_spec(row, namespace=namespace)
try:
    client.images.get(spec.instance_image_key)
except docker.errors.ImageNotFound:
    if spec.is_remote_image:
        client.images.pull(spec.instance_image_key)
    else:
        build_instance_images(
            client=client,
            dataset=[row],
            force_rebuild=False,
            max_workers=1,
            namespace=None,
            tag="latest",
            env_image_tag="latest",
        )
client.images.get(spec.instance_image_key)
payload = {{
    "image": spec.instance_image_key,
    "platform": spec.platform,
    "workdir": "/testbed",
}}
print({_AGENT_IMAGE_PREFIX!r} + json.dumps(payload, ensure_ascii=False))
"""


class SWEbenchBenchmark:
    """Prepare a base repository and delegate grading to SWE-bench Harness."""

    name = "swebench"

    def __init__(
        self,
        *,
        dataset_name: str = "princeton-nlp/SWE-bench_Verified",
        split: str = "test",
        source_workspace: Path | None = None,
        harness_python: str = sys.executable,
        grade_mode: str = "docker",
        cache_level: str = "env",
        clean: bool = False,
        namespace: str | None = "swebench",
        grade_timeout_seconds: int = 1_800,
        emit: Callable[[str], None] = print,
    ) -> None:
        if grade_mode not in {"docker", "none"}:
            raise ValueError("grade_mode must be 'docker' or 'none'")
        if cache_level not in {"none", "base", "env", "instance"}:
            raise ValueError("invalid SWE-bench cache level")
        self.dataset_name = dataset_name
        self.split = split
        self.source_workspace = (
            source_workspace.expanduser().resolve() if source_workspace else None
        )
        self.harness_python = harness_python
        self.grade_mode = grade_mode
        self.cache_level = cache_level
        self.clean = clean
        self.namespace = namespace
        self.grade_timeout_seconds = grade_timeout_seconds
        self.emit = emit

    def load_task(self, instance_id: str) -> EvalTask:
        if self.source_workspace is None:
            self.emit("Checking the Docker CLI and daemon for official SWE-bench.")
            _require_docker()
            self.emit(f"Loading public SWE-bench task {instance_id}.")
        row = _load_harness_instance(
            self.harness_python,
            self.dataset_name,
            self.split,
            instance_id,
        )
        required = ("instance_id", "repo", "base_commit", "problem_statement")
        missing = [key for key in required if not row.get(key)]
        if missing:
            raise ValueError(f"SWE-bench instance is missing fields: {missing}")
        if row["instance_id"] != instance_id:
            raise ValueError(
                f"SWE-bench Harness returned {row['instance_id']!r}, "
                f"not {instance_id!r}"
            )

        # Gold patches, hidden test patches, and test IDs deliberately do not cross
        # the public task boundary.
        metadata = {
            "repo": str(row["repo"]),
            "base_commit": str(row["base_commit"]),
            "version": row.get("version"),
            "difficulty": row.get("difficulty"),
            "dataset_revision": row.get("dataset_revision"),
            "created_at": row.get("created_at"),
            "environment_setup_commit": row.get("environment_setup_commit"),
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

        command_backend: dict[str, str] | None = None
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
            _require_head(workspace, base_commit)
        else:
            stdout_path = run_dir / "swebench-agent-image.stdout.log"
            stderr_path = run_dir / "swebench-agent-image.stderr.log"
            self.emit(
                "Preparing the public SWE-bench instance image; "
                "the first run may take several minutes."
            )
            self.emit(
                "Image preparation logs: "
                f"{stdout_path} (stdout), {stderr_path} (stderr)"
            )
            image, _, _ = _prepare_agent_image(
                self.harness_python,
                self.dataset_name,
                self.split,
                task.instance_id,
                self.namespace,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                emit=self.emit,
            )
            self.emit(
                f"Exporting {image['workdir']} from the prepared image "
                "into the artifact workspace."
            )
            _export_image_workspace(image, workspace)
            _require_base_ancestor(workspace, base_commit)
            self.emit(f"Prepared agent workspace: {workspace}")
            command_backend = {
                "type": "docker",
                "image": image["image"],
                "platform": image["platform"],
                "workdir": image["workdir"],
            }
        metadata: dict[str, Any] = {
            "repo": repo,
            "base_commit": base_commit,
        }
        if command_backend is not None:
            metadata["command_backend"] = command_backend
        return PreparedTask(
            task,
            workspace,
            metadata,
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
        stdout_path = run_dir / "swebench.stdout.log"
        stderr_path = run_dir / "swebench.stderr.log"
        self.emit(
            "Starting the official SWE-bench grader; this may take several minutes."
        )
        self.emit(
            f"Official grading logs: {stdout_path} (stdout), {stderr_path} (stderr)"
        )
        started = time.monotonic()
        timed_out = False
        try:
            process = _run_streaming(
                argv,
                cwd=run_dir,
                timeout=self.grade_timeout_seconds + 600,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                emit=self.emit,
                label="swebench grading",
            )
            return_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = 124
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


def _load_harness_instance(
    harness_python: str,
    dataset_name: str,
    split: str,
    instance_id: str,
) -> dict[str, Any]:
    try:
        process = subprocess.run(
            [harness_python, "-c", _INSTANCE_LOADER, dataset_name, split, instance_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"SWE-bench Harness Python does not exist: {harness_python}. "
            "Set SWEBENCH_PYTHON to its Python executable."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while loading the SWE-bench instance") from exc
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            "cannot load the SWE-bench instance with the official Harness. "
            f"Run Yada with `uv run --with '{_SWEBENCH_PACKAGE}' yada eval "
            "--swebench INSTANCE_ID ...`. "
            f"{detail}"
        )
    record = next(
        (
            line.removeprefix(_INSTANCE_PREFIX)
            for line in process.stdout.splitlines()
            if line.startswith(_INSTANCE_PREFIX)
        ),
        None,
    )
    if record is None:
        raise RuntimeError("official SWE-bench Harness returned no instance metadata")
    try:
        row = json.loads(record)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "official SWE-bench Harness returned invalid metadata"
        ) from exc
    if not isinstance(row, dict):
        raise RuntimeError("official SWE-bench Harness returned invalid metadata")
    return {key: row.get(key) for key in _PUBLIC_INSTANCE_FIELDS}


def _require_docker(docker_executable: str = "docker") -> None:
    if shutil.which(docker_executable) is None:
        raise RuntimeError(
            "official SWE-bench evaluation requires the `docker` CLI. "
            "Use `yada eval --case PATH` for a local no-Docker evaluation."
        )
    try:
        process = subprocess.run(
            [docker_executable, "info", "--format", "{{.ServerVersion}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "official SWE-bench evaluation requires a running Docker daemon. "
            "Use `yada eval --case PATH` for a local no-Docker evaluation."
        ) from exc
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            "official SWE-bench evaluation requires a running Docker daemon. "
            "Use `yada eval --case PATH` for a local no-Docker evaluation. "
            f"{detail}"
        )


def _prepare_agent_image(
    harness_python: str,
    dataset_name: str,
    split: str,
    instance_id: str,
    namespace: str | None,
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], str, str]:
    argv = [
        harness_python,
        "-u",
        "-c",
        _AGENT_IMAGE_PREPARER,
        dataset_name,
        split,
        instance_id,
        namespace if namespace is not None else "none",
    ]
    try:
        process = _run_streaming(
            argv,
            timeout=3_600,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            emit=emit,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"SWE-bench Harness Python does not exist: {harness_python}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "timed out while preparing the SWE-bench agent image"
            + _log_hint(stdout_path, stderr_path)
        ) from exc
    if process.returncode:
        detail = _tail(process.stderr.strip() or process.stdout.strip())
        raise RuntimeError(
            f"cannot prepare the SWE-bench agent image: {detail}"
            + _log_hint(stdout_path, stderr_path)
        )
    record = next(
        (
            line.removeprefix(_AGENT_IMAGE_PREFIX)
            for line in process.stdout.splitlines()
            if line.startswith(_AGENT_IMAGE_PREFIX)
        ),
        None,
    )
    if record is None:
        raise RuntimeError("SWE-bench Harness returned no agent image metadata")
    try:
        image = json.loads(record)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SWE-bench Harness returned invalid image metadata") from exc
    if not isinstance(image, dict) or not all(
        isinstance(image.get(key), str) and image[key]
        for key in ("image", "platform", "workdir")
    ):
        raise RuntimeError("SWE-bench Harness returned invalid image metadata")
    return image, process.stdout, process.stderr


def _run_streaming(
    argv: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    emit: Callable[[str], None] | None = None,
    heartbeat_seconds: float = 30,
    label: str = "swebench image",
) -> subprocess.CompletedProcess[str]:
    """Run a process while teeing both streams to durable logs and progress."""

    handles: dict[str, TextIO] = {}
    paths = {"stdout": stdout_path, "stderr": stderr_path}
    for name, path in paths.items():
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        handles[name] = path.open("w", encoding="utf-8")

    process: subprocess.Popen[str] | None = None
    output = {"stdout": [], "stderr": []}
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    threads: list[threading.Thread] = []
    started = time.monotonic()
    timed_out = False

    def read_stream(name: str, stream: TextIO) -> None:
        try:
            for line in iter(stream.readline, ""):
                events.put((name, line))
        finally:
            stream.close()
            events.put((name, None))

    def write_progress(message: str) -> None:
        handle = handles.get("stdout")
        if handle is not None:
            handle.write(message + "\n")
            handle.flush()
        if emit is not None:
            emit(message)

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        write_progress(f"[{label}] subprocess started")
        for name, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        ):
            thread = threading.Thread(
                target=read_stream,
                args=(name, stream),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        finished_streams: set[str] = set()
        deadline = started + timeout
        next_heartbeat = started + heartbeat_seconds
        while len(finished_streams) < 2:
            now = time.monotonic()
            if process.poll() is None and now >= deadline:
                process.kill()
                timed_out = True
            try:
                name, line = events.get(timeout=0.5)
            except queue.Empty:
                line = None
                name = ""
            if name and line is None:
                finished_streams.add(name)
            elif name:
                output[name].append(line)
                handle = handles.get(name)
                if handle is not None:
                    handle.write(line)
                    handle.flush()
                if emit is not None:
                    emit(f"[{label} {name}] {line.rstrip()}")
                next_heartbeat = time.monotonic() + heartbeat_seconds

            now = time.monotonic()
            if emit is not None and process.poll() is None and now >= next_heartbeat:
                elapsed = round(now - started)
                write_progress(
                    f"[{label}] still running "
                    f"({elapsed}s elapsed; live logs are being updated)"
                )
                next_heartbeat = now + heartbeat_seconds

        return_code = process.wait()
        stdout = "".join(output["stdout"])
        stderr = "".join(output["stderr"])
        if timed_out:
            write_progress(f"[{label}] timed out after {timeout}s")
            raise subprocess.TimeoutExpired(
                argv,
                timeout,
                output=stdout,
                stderr=stderr,
            )
        write_progress(f"[{label}] subprocess exited with status {return_code}")
        return subprocess.CompletedProcess(argv, return_code, stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for thread in threads:
            thread.join(timeout=1)
        for handle in handles.values():
            handle.close()


def _tail(value: str, limit: int = 4_000) -> str:
    return value if len(value) <= limit else "..." + value[-limit:]


def _log_hint(stdout_path: Path | None, stderr_path: Path | None) -> str:
    paths = [str(path) for path in (stdout_path, stderr_path) if path is not None]
    return f"; see {' and '.join(paths)}" if paths else ""


def _export_image_workspace(
    image: dict[str, str],
    workspace: Path,
    docker_executable: str = "docker",
) -> None:
    workspace.mkdir(parents=True)
    created = _run(
        [
            docker_executable,
            "create",
            "--platform",
            image["platform"],
            image["image"],
            "tail",
            "-f",
            "/dev/null",
        ],
        workspace.parent,
    )
    if created.returncode:
        raise RuntimeError(
            created.stderr.strip() or "cannot create the SWE-bench agent container"
        )
    container_id = created.stdout.strip()
    if not container_id:
        raise RuntimeError("Docker returned no SWE-bench agent container ID")
    try:
        copied = _run(
            [
                docker_executable,
                "cp",
                f"{container_id}:{image['workdir']}/.",
                str(workspace),
            ],
            workspace.parent,
            timeout=600,
        )
        if copied.returncode:
            raise RuntimeError(
                copied.stderr.strip() or "cannot export the SWE-bench workspace"
            )
    finally:
        _run(
            [docker_executable, "rm", "--force", container_id],
            workspace.parent,
        )
    if not (workspace / ".git").exists():
        raise RuntimeError("SWE-bench agent image contains no /testbed Git workspace")
    _run(["git", "config", "core.fileMode", "false"], workspace)


def _require_base_ancestor(workspace: Path, base_commit: str) -> None:
    exists = _run(["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], workspace)
    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"], workspace
    )
    if exists.returncode or ancestor.returncode:
        raise ValueError(
            f"prepared SWE-bench image is not based on expected commit {base_commit}"
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
