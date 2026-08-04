"""Command-line interface for benchmark-neutral evaluations."""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import sys
from pathlib import Path

from yada.editing import DEFAULT_EDITING_STRATEGY, EDITING_STRATEGY_CHOICES
from yada.evals.agents import CommandAgentAdapter, YadaAgentAdapter
from yada.evals.base import RunBudget
from yada.evals.benchmarks import LocalBenchmark, SWEbenchBenchmark
from yada.evals.runner import EvalRunner
from yada.utils.naming import next_available_run_name, readable_run_name


def build_parser() -> argparse.ArgumentParser:
    """Build the ``yada eval`` parser."""

    parser = argparse.ArgumentParser(
        prog="yada eval",
        description="Run one coding agent against one reproducible benchmark task.",
    )
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument(
        "--case",
        type=Path,
        help="Portable local case directory or case.json.",
    )
    task.add_argument(
        "--swebench",
        metavar="INSTANCE_ID",
        help=(
            "Official SWE-bench Verified instance to run and grade; "
            "requires a running Docker daemon."
        ),
    )
    parser.add_argument("--agent", choices=["yada", "command"], default="yada")
    parser.add_argument(
        "--agent-command",
        help=(
            "External agent command template. Available placeholders: {task}, "
            "{task_file}, {workspace}, {output_patch}, {run_dir}."
        ),
    )
    parser.add_argument("--agent-name", default="command")
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Result JSON path (default: eval-results/<task>__<system-local-time>.json)."
        ),
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Logs, workspace, patch, and prediction files for this run.",
    )
    parser.add_argument("--run-id", help="Stable run identifier for correlation.")

    budget = parser.add_argument_group("budget")
    budget.add_argument("--max-steps", type=int, default=30)
    budget.add_argument("--wall-time", type=int, default=1_800)
    budget.add_argument("--max-output-tokens", type=int, default=16_384)

    model = parser.add_argument_group("Yada / DeepSeek")
    model.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    )
    model.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model.add_argument("--reasoning-effort", choices=["high", "max"], default="max")
    model.add_argument(
        "--thinking",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    model.add_argument(
        "--editing-strategy",
        choices=EDITING_STRATEGY_CHOICES,
        default=DEFAULT_EDITING_STRATEGY.value,
        help="Run-level editing policy for the native Yada agent.",
    )
    model.add_argument("--api-timeout", type=int, default=300)
    model.add_argument("--command-timeout", type=int, default=120)
    model.add_argument(
        "--command-policy",
        choices=["ask", "allow", "deny"],
        default="ask",
    )
    model.add_argument("--yes", action="store_true")
    model.add_argument(
        "--trace-level",
        choices=["summary", "debug"],
        default="summary",
    )

    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Execute one evaluation and return a process-style status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.case is not None:
        case_path = args.case.expanduser().resolve()
        manifest_path = case_path / "case.json" if case_path.is_dir() else case_path
        if not manifest_path.is_file():
            parser.error(f"benchmark case does not exist: {manifest_path}")
        benchmark = LocalBenchmark(manifest_path)
        instance_id = _manifest_instance_id(benchmark)
        task_name = instance_id or case_path.stem
    else:
        benchmark = SWEbenchBenchmark(
            harness_python=os.environ.get("SWEBENCH_PYTHON", sys.executable),
            namespace=_default_swebench_namespace(),
        )
        instance_id = args.swebench
        assert instance_id is not None
        task_name = instance_id

    output_path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else _default_output_path(task_name).resolve()
    )
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else output_path.with_suffix("").with_name(output_path.stem + ".artifacts")
    )

    if args.agent == "yada":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            parser.error("DEEPSEEK_API_KEY is required for --agent yada")
        agent = YadaAgentAdapter(
            api_key=api_key,
            model=args.model,
            base_url=args.base_url,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
            api_timeout_seconds=args.api_timeout,
            command_timeout_seconds=args.command_timeout,
            command_policy="allow" if args.yes else args.command_policy,
            trace_level=args.trace_level,
            editing_strategy=args.editing_strategy,
        )
    else:
        if not args.agent_command:
            parser.error("--agent-command is required for --agent command")
        try:
            command = shlex.split(args.agent_command)
        except ValueError as exc:
            parser.error(f"invalid --agent-command: {exc}")
        agent = CommandAgentAdapter(
            command,
            name=args.agent_name,
            model=args.model,
        )

    runner = EvalRunner(
        benchmark=benchmark,
        agent=agent,
        budget=RunBudget(
            max_steps=args.max_steps,
            wall_time_seconds=args.wall_time,
            max_output_tokens=args.max_output_tokens,
        ),
        output_path=output_path,
        artifact_dir=artifact_dir,
        run_id=args.run_id,
    )
    result = runner.run(instance_id)
    print(f"Run: {result.run_id}")
    print(f"Benchmark: {result.benchmark}/{result.instance_id}")
    print(f"Agent: {result.agent}")
    print(f"Status: {result.status}")
    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)
    print(f"Result: {output_path}")
    print(f"Artifacts: {artifact_dir}")
    if result.status == "resolved":
        return 0
    if result.status == "unresolved":
        return 1
    return 2


def _default_output_path(task_name: str) -> Path:
    directory = Path("eval-results")
    run_name = next_available_run_name(
        directory,
        readable_run_name(task_name),
        suffixes=(".json", ".artifacts"),
    )
    return directory / f"{run_name}.json"


def _manifest_instance_id(benchmark: LocalBenchmark) -> str:
    value = benchmark.manifest.get("instance_id")
    return value if isinstance(value, str) else ""


def _default_swebench_namespace() -> str | None:
    if sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return None
    return "swebench"
