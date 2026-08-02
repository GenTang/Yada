"""Command-line interface for benchmark-neutral evaluations."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from yada.evals.agents import CommandAgentAdapter, YadaAgentAdapter
from yada.evals.base import RunBudget
from yada.evals.benchmarks import LocalBenchmark, SWEbenchBenchmark
from yada.evals.runner import EvalRunner


def build_parser() -> argparse.ArgumentParser:
    """Build the ``yada eval`` parser."""

    parser = argparse.ArgumentParser(
        prog="yada eval",
        description="Run one coding agent against one reproducible benchmark task.",
    )
    parser.add_argument("--benchmark", choices=["local", "swebench"], required=True)
    parser.add_argument("--instance", help="Benchmark instance ID.")
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
        default=_default_output_path(),
        help="Result JSON path.",
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
    model.add_argument("--api-timeout", type=int, default=300)
    model.add_argument("--command-timeout", type=int, default=120)
    model.add_argument(
        "--command-policy",
        choices=["ask", "allow", "deny"],
        default="ask",
    )
    model.add_argument("--yes", action="store_true")
    model.add_argument("--trace-reasoning", action="store_true")

    local = parser.add_argument_group("local benchmark")
    local.add_argument("--manifest", type=Path)

    swebench = parser.add_argument_group("SWE-bench")
    swebench.add_argument(
        "--dataset-name",
        default="SWE-bench/SWE-bench_Verified",
    )
    swebench.add_argument("--split", default="test")
    swebench.add_argument("--instance-file", type=Path)
    swebench.add_argument(
        "--workspace",
        type=Path,
        help="Optional clean source repo to clone instead of fetching GitHub.",
    )
    swebench.add_argument("--swebench-python", default=sys.executable)
    swebench.add_argument("--grade-mode", choices=["docker", "none"], default="docker")
    swebench.add_argument(
        "--cache-level",
        choices=["none", "base", "env", "instance"],
        default="env",
    )
    swebench.add_argument("--clean", action="store_true")
    swebench.add_argument(
        "--namespace",
        default="swebench",
        help="Docker image namespace; use 'none' to build locally.",
    )
    swebench.add_argument("--grade-timeout", type=int, default=1_800)
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Execute one evaluation and return a process-style status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    output_path = args.output.expanduser().resolve()
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else output_path.with_suffix("").with_name(output_path.stem + ".artifacts")
    )

    if args.benchmark == "local":
        if args.manifest is None:
            parser.error("--manifest is required for --benchmark local")
        benchmark = LocalBenchmark(args.manifest)
        instance_id = args.instance or ""
    else:
        if not args.instance:
            parser.error("--instance is required for --benchmark swebench")
        namespace = None if args.namespace.lower() == "none" else args.namespace
        benchmark = SWEbenchBenchmark(
            dataset_name=args.dataset_name,
            split=args.split,
            instance_file=args.instance_file,
            source_workspace=args.workspace,
            harness_python=args.swebench_python,
            grade_mode=args.grade_mode,
            cache_level=args.cache_level,
            clean=args.clean,
            namespace=namespace,
            grade_timeout_seconds=args.grade_timeout,
        )
        instance_id = args.instance

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
            include_reasoning=args.trace_reasoning,
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


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("eval-results") / f"{timestamp}.json"
