"""Command-line interface for Yada."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from yada import __version__
from yada.agents import Agent
from yada.models import DeepSeekAPIError, DeepSeekClient
from yada.tools import ToolRunner
from yada.traces import TraceWriter


def build_parser() -> argparse.ArgumentParser:
    """Build the public ``yada`` command-line parser."""

    parser = argparse.ArgumentParser(
        prog="yada",
        description="Yet Another DeepSeek Agent: a small DeepSeek-native coding agent.",
        epilog="Use 'yada eval --help' to run reproducible benchmark evaluations.",
    )
    parser.add_argument("task", nargs="?", help="Coding task to complete.")
    parser.add_argument("--task-file", type=Path, help="Read the task from a UTF-8 file.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root (default: cwd).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        help="DeepSeek model name.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        help="DeepSeek API base URL.",
    )
    parser.add_argument(
        "--reasoning-effort", choices=["high", "max"], default="max"
    )
    parser.add_argument(
        "--thinking", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-output-tokens", type=int, default=16_384)
    parser.add_argument("--api-timeout", type=int, default=300)
    parser.add_argument("--command-timeout", type=int, default=120)
    parser.add_argument(
        "--command-policy",
        choices=["ask", "allow", "deny"],
        default="ask",
        help="ask by default; allow is autonomous and should be used in a sandbox.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Alias for --command-policy allow. Run only in a trusted sandbox.",
    )
    parser.add_argument(
        "--trace",
        type=Path,
        help="JSONL trace path (default: WORKSPACE/.yada/runs/<timestamp>.jsonl).",
    )
    parser.add_argument(
        "--trace-reasoning",
        action="store_true",
        help="Store full reasoning_content in the trace; redacted by default.",
    )
    parser.add_argument("--version", action="version", version=f"yada {__version__}")
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Parse arguments, assemble the runtime, and execute one task.

    Args:
        argv: Optional arguments excluding the executable name.

    Returns:
        ``0`` on verified completion, ``2`` when unfinished, ``3`` for a model
        API error, and ``130`` when interrupted by the user.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["eval"]:
        from yada.evals.cli import run_cli as run_eval_cli

        return run_eval_cli(arguments[1:])

    parser = build_parser()
    args = parser.parse_args(arguments)
    if bool(args.task) == bool(args.task_file):
        parser.error("provide exactly one of TASK or --task-file")
    if args.task_file:
        try:
            task = args.task_file.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read task file: {exc}")
    else:
        task = args.task
    if not task or not task.strip():
        parser.error("task must not be empty")

    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        parser.error("DEEPSEEK_API_KEY is not set")

    trace_path = args.trace or _default_trace_path(workspace)
    command_policy = "allow" if args.yes else args.command_policy
    agent = Agent(
        client=DeepSeekClient(
            api_key=api_key,
            base_url=args.base_url,
            model=args.model,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.api_timeout,
        ),
        tools=ToolRunner(
            workspace,
            command_policy=command_policy,
            command_timeout_seconds=args.command_timeout,
        ),
        trace=TraceWriter(trace_path, include_reasoning=args.trace_reasoning),
        max_steps=args.max_steps,
    )

    print(f"Yada {__version__}")
    print(f"Workspace: {workspace}")
    print(
        f"Model: {args.model} "
        f"(thinking={args.thinking}, effort={args.reasoning_effort})"
    )
    print(f"Trace: {trace_path}")
    if command_policy == "allow":
        print("WARNING: command execution is autonomous; use a container for untrusted repos.")

    try:
        result = agent.run(task)
    except DeepSeekAPIError as exc:
        print(f"\nDeepSeek API error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    print("\n=== Yada result ===")
    print(f"Finished: {result.finished}")
    print(f"Steps: {result.steps}")
    print(f"Summary: {result.summary}")
    if result.usage:
        print("Usage:")
        for key, value in sorted(result.usage.items()):
            print(f"  {key}: {value}")
    if result.final_state.get("diff_stat"):
        print("Diff stat:")
        print(result.final_state["diff_stat"].rstrip())
    print(f"Trace: {trace_path}")
    return 0 if result.finished else 2


def _default_trace_path(workspace: Path) -> Path:
    # Microseconds prevent two short runs launched in the same second from
    # appending unrelated events to one file.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return workspace / ".yada" / "runs" / f"{timestamp}.jsonl"


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run_cli())
