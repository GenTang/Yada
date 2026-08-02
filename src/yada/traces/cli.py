"""Command-line interface for inspecting a Yada trace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from yada.traces.report import TraceFormatError, render_trace_report


def build_parser() -> argparse.ArgumentParser:
    """Create the parser for the lightweight ``yada-trace`` command."""

    parser = argparse.ArgumentParser(
        prog="yada-trace",
        description="Summarize a Yada JSONL run as a correlated timeline.",
    )
    parser.add_argument("trace", type=Path, help="Path to a Yada JSONL trace.")
    parser.add_argument(
        "--step",
        type=_positive_step,
        help="Show full model and tool details for one agent step.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Expand event payloads for the complete timeline.",
    )
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Render a trace report and return a process-style exit status."""

    args = build_parser().parse_args(argv)
    try:
        report = render_trace_report(
            args.trace.expanduser().resolve(),
            step=args.step,
            verbose=args.verbose,
        )
    except (OSError, TraceFormatError) as exc:
        print(f"yada-trace: {exc}", file=sys.stderr)
        return 2
    print(report, end="")
    return 0


def _positive_step(value: str) -> int:
    try:
        step = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("step must be a positive integer") from exc
    if step < 1:
        raise argparse.ArgumentTypeError("step must be a positive integer")
    return step


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run_cli())
