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
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Render a trace report and return a process-style exit status."""

    args = build_parser().parse_args(argv)
    try:
        report = render_trace_report(args.trace.expanduser().resolve())
    except (OSError, TraceFormatError) as exc:
        print(f"yada-trace: {exc}", file=sys.stderr)
        return 2
    print(report, end="")
    return 0


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run_cli())
