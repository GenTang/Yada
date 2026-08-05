#!/usr/bin/env python3
"""Run a versioned SWE-bench suite through Yada's single-task eval command.

This development script deliberately uses only the Python standard library. It
keeps suite orchestration outside ``src/yada`` and treats every underlying
``python -m yada eval --swebench`` invocation as an isolated, durable attempt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SUITE_RUN_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
ATTEMPT_SCHEMA_VERSION = 1
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BUDGETS = {
    "max_steps": 30,
    "wall_time_seconds": 1_800,
    "max_output_tokens": 16_384,
}
_SUITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?P<context>.*)$")


class SuiteError(RuntimeError):
    """A user-facing suite configuration or state error."""


@dataclass(frozen=True)
class SuiteManifest:
    """Validated fields from one versioned suite manifest."""

    path: Path
    suite_id: str
    benchmark: str
    instances: tuple[str, ...]
    sha256: str
    raw: bytes


def build_parser() -> argparse.ArgumentParser:
    """Build the development-only suite runner parser."""

    parser = argparse.ArgumentParser(
        description="Run and resume a versioned SWE-bench evaluation suite."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run",
        help="Run missing attempts sequentially and refresh suite summaries.",
    )
    run.add_argument("manifest", type=Path, help="Versioned suite JSON manifest.")
    destination = run.add_mutually_exclusive_group()
    destination.add_argument(
        "--output-dir",
        "--output",
        type=Path,
        help=("Suite directory. An existing directory with suite-run.json is resumed."),
    )
    destination.add_argument(
        "--resume",
        type=Path,
        help="Existing suite directory to resume.",
    )
    run.add_argument(
        "--repeat",
        type=_positive_int,
        default=None,
        help="Run every instance N times (default: 1; no per-instance overrides).",
    )
    run.add_argument("--model", default=None)
    run.add_argument(
        "--api-key-file",
        type=Path,
        default=None,
        help="Private DeepSeek API key file forwarded to every Yada attempt.",
    )
    run.add_argument("--max-steps", type=_positive_int, default=None)
    run.add_argument("--wall-time", type=_positive_int, default=None)
    run.add_argument("--max-output-tokens", type=_positive_int, default=None)
    run.add_argument("--reasoning-effort", choices=("high", "max"), default=None)
    run.add_argument(
        "--thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    run.add_argument(
        "--editing-strategy",
        choices=("patch-only", "replace-first"),
        default=None,
    )
    run.add_argument("--api-timeout", type=_positive_int, default=None)
    run.add_argument("--command-timeout", type=_positive_int, default=None)
    run.add_argument("--trace-level", choices=("summary", "debug"), default=None)
    run.add_argument(
        "--python",
        dest="python_executable",
        default=None,
        help=("Python used for 'python -m yada eval' (default: this interpreter)."),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command and return a process-style exit code."""

    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return run_suite(args)
    except SuiteError as exc:
        print(f"eval-suite: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unsupported command: {args.command}")


def run_suite(args: argparse.Namespace) -> int:
    """Create or resume one suite directory, running all missing attempts."""

    manifest = load_manifest(args.manifest)
    repository = Path(__file__).resolve().parents[1]
    yada_commit = git_head(repository)
    suite_dir, metadata = _open_suite_run(
        args,
        manifest=manifest,
        repository=repository,
        yada_commit=yada_commit,
    )
    repeat = _require_int(metadata["configuration"], "repeat")
    total = len(manifest.instances) * repeat
    print(f"Suite: {manifest.suite_id}")
    print(f"Directory: {suite_dir}")
    print(f"Attempts: {total} ({len(manifest.instances)} instances x {repeat})")

    try:
        for instance_index, instance_id in enumerate(manifest.instances, 1):
            for attempt_number in range(1, repeat + 1):
                attempt_dir = _attempt_dir(
                    suite_dir,
                    instance_index,
                    instance_id,
                    attempt_number,
                )
                marker = attempt_dir / "attempt.json"
                if marker.is_file():
                    _load_attempt(marker, instance_id, attempt_number)
                    print(
                        f"Skip completed: {instance_id} "
                        f"(attempt {attempt_number}/{repeat})"
                    )
                    continue

                recovered = _recover_attempt(
                    suite_dir,
                    attempt_dir,
                    instance_id,
                    attempt_number,
                )
                if recovered is not None:
                    _write_json(marker, recovered)
                    print(
                        f"Recovered completed: {instance_id} "
                        f"(attempt {attempt_number}/{repeat})"
                    )
                    write_summaries(suite_dir, manifest, metadata)
                    continue

                execution_dir = _next_execution_dir(attempt_dir)
                print(
                    f"Run: {instance_id} (attempt {attempt_number}/{repeat}, "
                    f"{execution_dir.name})"
                )
                record = _run_attempt(
                    suite_dir=suite_dir,
                    execution_dir=execution_dir,
                    instance_id=instance_id,
                    instance_index=instance_index,
                    attempt_number=attempt_number,
                    metadata=metadata,
                    repository=repository,
                )
                _write_json(marker, record)
                write_summaries(suite_dir, manifest, metadata)
                print(f"Outcome: {record['status']}")
    except KeyboardInterrupt:
        write_summaries(suite_dir, manifest, metadata)
        print(
            f"\nInterrupted. Resume with --resume {suite_dir}",
            file=sys.stderr,
        )
        return 130

    summary = write_summaries(suite_dir, manifest, metadata)
    counts = summary["counts"]
    print(
        "Complete: "
        f"{counts['resolved']} resolved, {counts['unresolved']} unresolved, "
        f"{counts['error']} error"
    )
    print(f"Summary: {suite_dir / 'summary.json'}")
    return 0


def load_manifest(path: Path) -> SuiteManifest:
    """Load and strictly validate a suite manifest."""

    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise SuiteError(f"cannot read suite manifest {resolved}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuiteError(f"invalid UTF-8 JSON suite manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise SuiteError("suite manifest must be a JSON object")
    if data.get("schema_version") != 1:
        raise SuiteError("suite manifest schema_version must be 1")
    suite_id = data.get("suite_id")
    if not isinstance(suite_id, str) or not _SUITE_ID_PATTERN.fullmatch(suite_id):
        raise SuiteError("suite_id must contain only letters, digits, '.', '_' or '-'")
    benchmark = data.get("benchmark")
    if benchmark != "swebench-verified":
        raise SuiteError("benchmark must be 'swebench-verified'")
    instances = data.get("instances")
    if not isinstance(instances, list) or not instances:
        raise SuiteError("instances must be a non-empty array")
    if any(
        not isinstance(item, str) or not _INSTANCE_ID_PATTERN.fullmatch(item)
        for item in instances
    ):
        raise SuiteError("each instance ID must be a path-safe non-empty string")
    if len(set(instances)) != len(instances):
        raise SuiteError("suite instance IDs must be unique")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return SuiteManifest(
        path=resolved,
        suite_id=suite_id,
        benchmark=benchmark,
        instances=tuple(instances),
        sha256=digest,
        raw=raw,
    )


def git_head(repository: Path) -> str | None:
    """Return the repository commit without making Git a run dependency."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def semantic_patch_id(patch: str) -> str:
    """Hash a canonical unified diff while ignoring non-semantic Git metadata."""

    canonical: list[str] = []
    for line in patch.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if line.startswith(("index ", "--- ", "+++ ")):
            continue
        hunk = _HUNK_PATTERN.match(line)
        if hunk:
            canonical.append("@@" + hunk.group("context"))
        else:
            canonical.append(line)
    payload = "\n".join(canonical).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def write_summaries(
    suite_dir: Path,
    manifest: SuiteManifest,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Regenerate deterministic JSON and Markdown from durable attempt markers."""

    summary = build_summary(suite_dir, manifest, metadata)
    _write_json(suite_dir / "summary.json", summary)
    _write_text(suite_dir / "summary.md", render_markdown(summary))
    return summary


def build_summary(
    suite_dir: Path,
    manifest: SuiteManifest,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic aggregate in manifest and attempt order."""

    configuration = metadata["configuration"]
    repeat = _require_int(configuration, "repeat")
    all_attempts: list[dict[str, Any]] = []
    instance_summaries: list[dict[str, Any]] = []
    for instance_index, instance_id in enumerate(manifest.instances, 1):
        attempts: list[dict[str, Any]] = []
        for attempt_number in range(1, repeat + 1):
            marker = (
                _attempt_dir(
                    suite_dir,
                    instance_index,
                    instance_id,
                    attempt_number,
                )
                / "attempt.json"
            )
            if marker.is_file():
                attempts.append(_load_attempt(marker, instance_id, attempt_number))
        all_attempts.extend(attempts)
        counts = _outcome_counts(attempts)
        patch_ids = sorted(
            {
                item["patch_id"]
                for item in attempts
                if isinstance(item.get("patch_id"), str)
            }
        )
        converged: bool | None
        if repeat == 1 or len(attempts) < repeat:
            converged = None
        elif any(not isinstance(item.get("patch_id"), str) for item in attempts):
            converged = False
        else:
            converged = len(patch_ids) == 1
        instance_summaries.append(
            {
                "instance_id": instance_id,
                "expected_attempts": repeat,
                "completed_attempts": len(attempts),
                "pending_attempts": repeat - len(attempts),
                "counts": counts,
                "resolution_rate": _resolution_rate(counts),
                "metrics": _metrics(attempts),
                "patch_ids": patch_ids,
                "patches_converged": converged,
                "attempts": [_attempt_summary(item) for item in attempts],
            }
        )
    counts = _outcome_counts(all_attempts)
    expected = len(manifest.instances) * repeat
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "suite_id": manifest.suite_id,
        "benchmark": manifest.benchmark,
        "suite_sha256": manifest.sha256,
        "yada_commit": metadata["yada_commit"],
        "model": configuration["model"],
        "budgets": configuration["budgets"],
        "repeat": repeat,
        "expected_attempts": expected,
        "completed_attempts": len(all_attempts),
        "pending_attempts": expected - len(all_attempts),
        "counts": counts,
        "resolution_rate": _resolution_rate(counts),
        "metrics": _metrics(all_attempts),
        "instances": instance_summaries,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    """Render a stable human-readable suite summary."""

    counts = summary["counts"]
    budgets = summary["budgets"]
    lines = [
        f"# SWE-bench suite summary: {summary['suite_id']}",
        "",
        (
            "Development canary only; these results are not an official "
            "leaderboard score."
        ),
        "",
        f"- Suite hash: `{summary['suite_sha256']}`",
        f"- Yada commit: `{summary['yada_commit'] or 'unknown'}`",
        f"- Model: `{summary['model']}`",
        (
            "- Budgets: "
            f"steps={budgets['max_steps']}, "
            f"wall={budgets['wall_time_seconds']}s, "
            f"output tokens={budgets['max_output_tokens']}"
        ),
        f"- Repeat: {summary['repeat']} per instance",
        (
            f"- Progress: {summary['completed_attempts']}/"
            f"{summary['expected_attempts']} attempts"
        ),
        "",
        "## Overall",
        "",
        "| Resolved | Unresolved | Error | Resolution rate |",
        "| ---: | ---: | ---: | ---: |",
        (
            f"| {counts['resolved']} | {counts['unresolved']} | "
            f"{counts['error']} | {_percent(summary['resolution_rate'])} |"
        ),
        "",
        "| Agent metric | Min | Max | Median |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, key in (
        ("Steps", "steps"),
        ("Tokens", "tokens"),
        ("Duration (ms)", "agent_duration_ms"),
    ):
        values = summary["metrics"][key]
        lines.append(
            f"| {label} | {_value(values['min'])} | {_value(values['max'])} | "
            f"{_value(values['median'])} |"
        )

    lines.extend(
        [
            "",
            "## Per-instance results",
            "",
            (
                "| Instance | R/U/E | Rate | Steps min/max/median | "
                "Tokens min/max/median | Agent ms min/max/median | "
                "Patches converge |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for instance in summary["instances"]:
        metrics = instance["metrics"]
        count = instance["counts"]
        lines.append(
            f"| `{_escape(instance['instance_id'])}` | "
            f"{count['resolved']}/{count['unresolved']}/{count['error']} | "
            f"{_percent(instance['resolution_rate'])} | "
            f"{_range_text(metrics['steps'])} | "
            f"{_range_text(metrics['tokens'])} | "
            f"{_range_text(metrics['agent_duration_ms'])} | "
            f"{_convergence_text(instance['patches_converged'])} |"
        )

    lines.extend(
        [
            "",
            "## Attempts",
            "",
            (
                "| Instance | Attempt | Outcome | Steps | Tokens | Agent ms | "
                "Patch ID | Result | Trace |"
            ),
            "| --- | ---: | --- | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for instance in summary["instances"]:
        for attempt in instance["attempts"]:
            lines.append(
                f"| `{_escape(instance['instance_id'])}` | "
                f"{attempt['attempt']} | {attempt['status']} | "
                f"{_value(attempt['steps'])} | {_value(attempt['tokens'])} | "
                f"{_value(attempt['agent_duration_ms'])} | "
                f"{_code(attempt['patch_id'])} | "
                f"{_path_link(attempt['result_path'], 'result')} | "
                f"{_path_link(attempt['trace_path'], 'trace')} |"
            )
    return "\n".join(lines) + "\n"


def _open_suite_run(
    args: argparse.Namespace,
    *,
    manifest: SuiteManifest,
    repository: Path,
    yada_commit: str | None,
) -> tuple[Path, dict[str, Any]]:
    requested = args.resume or args.output_dir
    if requested is None:
        suite_dir = _next_default_suite_dir(repository, manifest.suite_id)
        existing = False
    else:
        suite_dir = requested.expanduser().resolve()
        existing = (suite_dir / "suite-run.json").is_file()
        if args.resume is not None and not existing:
            raise SuiteError(f"resume directory has no suite-run.json: {suite_dir}")

    if existing:
        metadata = _read_json(suite_dir / "suite-run.json")
        _validate_resume(
            args,
            metadata=metadata,
            manifest=manifest,
            yada_commit=yada_commit,
        )
        return suite_dir, metadata

    configuration = _new_configuration(args)
    if suite_dir.exists() and any(suite_dir.iterdir()):
        raise SuiteError(f"new suite directory is not empty: {suite_dir}")
    suite_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": SUITE_RUN_SCHEMA_VERSION,
        "suite_id": manifest.suite_id,
        "benchmark": manifest.benchmark,
        "suite_sha256": manifest.sha256,
        "manifest_path": _display_path(manifest.path, repository),
        "instances": list(manifest.instances),
        "yada_commit": yada_commit,
        "configuration": configuration,
        "created_at": _utc_now(),
    }
    _write_bytes(suite_dir / "suite-manifest.json", manifest.raw)
    _write_json(suite_dir / "suite-run.json", metadata)
    return suite_dir, metadata


def _new_configuration(args: argparse.Namespace) -> dict[str, Any]:
    repeat = args.repeat if args.repeat is not None else 1
    model = args.model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise SuiteError("model must not be empty")
    return {
        "repeat": repeat,
        "model": model,
        "api_key_file": (
            str(args.api_key_file.expanduser().resolve())
            if args.api_key_file is not None
            else None
        ),
        "budgets": {
            "max_steps": args.max_steps or DEFAULT_BUDGETS["max_steps"],
            "wall_time_seconds": (
                args.wall_time or DEFAULT_BUDGETS["wall_time_seconds"]
            ),
            "max_output_tokens": (
                args.max_output_tokens or DEFAULT_BUDGETS["max_output_tokens"]
            ),
        },
        "reasoning_effort": args.reasoning_effort or "max",
        "thinking": True if args.thinking is None else args.thinking,
        "editing_strategy": args.editing_strategy or "replace-first",
        "api_timeout_seconds": args.api_timeout or 300,
        "command_timeout_seconds": args.command_timeout or 120,
        "trace_level": args.trace_level or "summary",
        "python_executable": args.python_executable or sys.executable,
        "autonomous_commands": True,
    }


def _validate_resume(
    args: argparse.Namespace,
    *,
    metadata: dict[str, Any],
    manifest: SuiteManifest,
    yada_commit: str | None,
) -> None:
    if metadata.get("schema_version") != SUITE_RUN_SCHEMA_VERSION:
        raise SuiteError("unsupported suite-run.json schema_version")
    expected = {
        "suite_id": manifest.suite_id,
        "benchmark": manifest.benchmark,
        "suite_sha256": manifest.sha256,
        "instances": list(manifest.instances),
        "yada_commit": yada_commit,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise SuiteError(
                f"cannot resume: {key} changed ({metadata.get(key)!r} != {value!r})"
            )
    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise SuiteError("suite-run.json configuration must be an object")
    stored_budgets = configuration.get("budgets")
    if not isinstance(stored_budgets, dict):
        raise SuiteError("suite-run.json budgets must be an object")
    supplied = {
        "repeat": args.repeat,
        "model": args.model,
        "api_key_file": (
            str(args.api_key_file.expanduser().resolve())
            if args.api_key_file is not None
            else None
        ),
        "reasoning_effort": args.reasoning_effort,
        "thinking": args.thinking,
        "editing_strategy": args.editing_strategy,
        "api_timeout_seconds": args.api_timeout,
        "command_timeout_seconds": args.command_timeout,
        "trace_level": args.trace_level,
        "python_executable": args.python_executable,
    }
    for key, value in supplied.items():
        if value is not None and configuration.get(key) != value:
            raise SuiteError(f"cannot resume with a different {key}")
    supplied_budgets = {
        "max_steps": args.max_steps,
        "wall_time_seconds": args.wall_time,
        "max_output_tokens": args.max_output_tokens,
    }
    for key, value in supplied_budgets.items():
        if value is not None and stored_budgets.get(key) != value:
            raise SuiteError(f"cannot resume with a different {key}")


def _run_attempt(
    *,
    suite_dir: Path,
    execution_dir: Path,
    instance_id: str,
    instance_index: int,
    attempt_number: int,
    metadata: dict[str, Any],
    repository: Path,
) -> dict[str, Any]:
    execution_dir.mkdir(parents=True, exist_ok=False)
    result_path = execution_dir / "result.json"
    artifacts_path = execution_dir / "artifacts"
    configuration = metadata["configuration"]
    budgets = configuration["budgets"]
    run_id = (
        f"{metadata['suite_id']}-{instance_index:03d}-"
        f"r{attempt_number:03d}-{execution_dir.name}"
    )
    command = [
        str(configuration["python_executable"]),
        "-m",
        "yada",
        "eval",
        "--swebench",
        instance_id,
        "--output",
        str(result_path),
        "--artifact-dir",
        str(artifacts_path),
        "--run-id",
        run_id,
        "--model",
        str(configuration["model"]),
        "--max-steps",
        str(budgets["max_steps"]),
        "--wall-time",
        str(budgets["wall_time_seconds"]),
        "--max-output-tokens",
        str(budgets["max_output_tokens"]),
        "--reasoning-effort",
        str(configuration["reasoning_effort"]),
        "--thinking" if configuration["thinking"] else "--no-thinking",
        "--editing-strategy",
        str(configuration["editing_strategy"]),
        "--api-timeout",
        str(configuration["api_timeout_seconds"]),
        "--command-timeout",
        str(configuration["command_timeout_seconds"]),
        "--trace-level",
        str(configuration["trace_level"]),
        "--yes",
    ]
    if configuration.get("api_key_file"):
        command.extend(["--api-key-file", str(configuration["api_key_file"])])
    started_at = _utc_now()
    started = time.monotonic()
    launch_error: str | None = None
    return_code: int | None = None
    try:
        process = subprocess.run(command, cwd=repository, check=False)
        return_code = process.returncode
    except KeyboardInterrupt:
        raise
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
    duration_ms = round((time.monotonic() - started) * 1000)
    return _attempt_record(
        suite_dir=suite_dir,
        result_path=result_path,
        artifacts_path=artifacts_path,
        instance_id=instance_id,
        attempt_number=attempt_number,
        execution=execution_dir.name,
        return_code=return_code,
        started_at=started_at,
        completed_at=_utc_now(),
        suite_duration_ms=duration_ms,
        recovered=False,
        external_error=launch_error,
    )


def _recover_attempt(
    suite_dir: Path,
    attempt_dir: Path,
    instance_id: str,
    attempt_number: int,
) -> dict[str, Any] | None:
    if not attempt_dir.is_dir():
        return None
    executions = sorted(
        (
            item
            for item in attempt_dir.iterdir()
            if item.is_dir() and re.fullmatch(r"execution-\d{3}", item.name)
        ),
        reverse=True,
    )
    for execution_dir in executions:
        result_path = execution_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            result = _read_json(result_path)
        except SuiteError:
            continue
        if result.get("instance_id") != instance_id:
            continue
        return _attempt_record(
            suite_dir=suite_dir,
            result_path=result_path,
            artifacts_path=execution_dir / "artifacts",
            instance_id=instance_id,
            attempt_number=attempt_number,
            execution=execution_dir.name,
            return_code=None,
            started_at=str(result.get("started_at") or "unknown"),
            completed_at=_timestamp_for(result_path),
            suite_duration_ms=_optional_int(result.get("duration_ms")),
            recovered=True,
            external_error=None,
        )
    return None


def _attempt_record(
    *,
    suite_dir: Path,
    result_path: Path,
    artifacts_path: Path,
    instance_id: str,
    attempt_number: int,
    execution: str,
    return_code: int | None,
    started_at: str,
    completed_at: str,
    suite_duration_ms: int | None,
    recovered: bool,
    external_error: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    result_error = external_error
    if result_path.is_file():
        try:
            candidate = _read_json(result_path)
            if candidate.get("instance_id") != instance_id:
                result_error = (
                    "result instance mismatch: "
                    f"{candidate.get('instance_id')!r} != {instance_id!r}"
                )
            else:
                result = candidate
        except SuiteError as exc:
            result_error = str(exc)
    elif result_error is None:
        result_error = f"evaluation exited with code {return_code} without result.json"

    raw_status = result.get("status") if result else None
    status = raw_status if raw_status in {"resolved", "unresolved"} else "error"
    agent_run = result.get("agent_run") if result else None
    if not isinstance(agent_run, dict):
        agent_run = {}
    usage = _numeric_usage(agent_run.get("usage"))
    patch = agent_run.get("patch")
    patch_id = semantic_patch_id(patch) if isinstance(patch, str) else None
    trace_path = agent_run.get("trace_path")
    if not isinstance(trace_path, str):
        fallback_trace = artifacts_path / "yada-trace.jsonl"
        trace_path = str(fallback_trace) if fallback_trace.is_file() else None
    error = result_error
    if error is None and status == "error" and result:
        value = result.get("error")
        error = str(value) if value else f"evaluation status: {raw_status!r}"
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "instance_id": instance_id,
        "attempt": attempt_number,
        "execution": execution,
        "status": status,
        "return_code": return_code,
        "started_at": started_at,
        "completed_at": completed_at,
        "suite_duration_ms": suite_duration_ms,
        "model": agent_run.get("model"),
        "steps": _optional_int(agent_run.get("steps")),
        "usage": usage,
        "tokens": _token_count(usage),
        "agent_duration_ms": _optional_int(agent_run.get("duration_ms")),
        "patch_id": patch_id,
        "result_path": _relative_path(result_path, suite_dir),
        "artifacts_path": _relative_path(artifacts_path, suite_dir),
        "trace_path": _relative_path(Path(trace_path), suite_dir)
        if trace_path
        else None,
        "error": _redact_text(error) if error else None,
        "recovered": recovered,
    }


def _attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "attempt",
        "execution",
        "status",
        "return_code",
        "steps",
        "tokens",
        "usage",
        "agent_duration_ms",
        "suite_duration_ms",
        "patch_id",
        "result_path",
        "artifacts_path",
        "trace_path",
        "error",
        "recovered",
    )
    return {key: attempt.get(key) for key in keys}


def _metrics(attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: _metric_range([item.get(key) for item in attempts])
        for key in ("steps", "tokens", "agent_duration_ms")
    }


def _metric_range(values: list[Any]) -> dict[str, int | float | None]:
    numbers = [
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if not numbers:
        return {"min": None, "max": None, "median": None}
    return {
        "min": min(numbers),
        "max": max(numbers),
        "median": statistics.median(numbers),
    }


def _outcome_counts(attempts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(item.get("status") == status for item in attempts)
        for status in ("resolved", "unresolved", "error")
    }


def _resolution_rate(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    return round(counts["resolved"] / total, 6) if total else 0.0


def _numeric_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        if isinstance(item, int) and not isinstance(item, bool)
    }


def _token_count(usage: dict[str, int]) -> int | None:
    if "total_tokens" in usage:
        return usage["total_tokens"]
    for left, right in (
        ("input_tokens", "output_tokens"),
        ("prompt_tokens", "completion_tokens"),
    ):
        if left in usage or right in usage:
            return usage.get(left, 0) + usage.get(right, 0)
    return None


def _load_attempt(
    path: Path,
    instance_id: str,
    attempt_number: int,
) -> dict[str, Any]:
    data = _read_json(path)
    if data.get("schema_version") != ATTEMPT_SCHEMA_VERSION:
        raise SuiteError(f"unsupported attempt schema: {path}")
    if data.get("instance_id") != instance_id or data.get("attempt") != attempt_number:
        raise SuiteError(f"attempt marker identity mismatch: {path}")
    if data.get("status") not in {"resolved", "unresolved", "error"}:
        raise SuiteError(f"attempt marker has invalid status: {path}")
    return data


def _attempt_dir(
    suite_dir: Path,
    instance_index: int,
    instance_id: str,
    attempt_number: int,
) -> Path:
    return (
        suite_dir
        / "runs"
        / f"{instance_index:03d}-{instance_id}"
        / f"attempt-{attempt_number:03d}"
    )


def _next_execution_dir(attempt_dir: Path) -> Path:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    number = 1
    while (attempt_dir / f"execution-{number:03d}").exists():
        number += 1
    return attempt_dir / f"execution-{number:03d}"


def _next_default_suite_dir(repository: Path, suite_id: str) -> Path:
    root = repository / "eval-results" / "suites"
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = root / f"{suite_id}__{timestamp}"
    number = 1
    while candidate.exists():
        candidate = root / f"{suite_id}__{timestamp}({number})"
        number += 1
    return candidate.resolve()


def _relative_path(path: Path, root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _display_path(path: Path, repository: Path) -> str:
    try:
        return path.relative_to(repository).as_posix()
    except ValueError:
        return str(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SuiteError(f"expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    _write_bytes(path, value.encode("utf-8"))


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _require_int(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SuiteError(f"configuration {key} must be a positive integer")
    return value


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _timestamp_for(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", redacted)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _range_text(metric: dict[str, Any]) -> str:
    return "/".join(_value(metric[key]) for key in ("min", "max", "median"))


def _convergence_text(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("`", "\\`")


def _code(value: str | None) -> str:
    return f"`{_escape(value)}`" if value else "—"


def _path_link(value: str | None, label: str) -> str:
    if not value:
        return "—"
    return f"[{label}]({_escape(value)})"


if __name__ == "__main__":
    raise SystemExit(main())
