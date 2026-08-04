"""Command-line interface for inspecting a Yada trace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from yada.traces.html import write_trace_html
from yada.traces.report import TraceFormatError, render_trace_report


def build_parser() -> argparse.ArgumentParser:
    """Create the parser for the lightweight ``yada-trace`` command."""

    parser = argparse.ArgumentParser(
        prog="yada-trace",
        description="Summarize a Yada JSONL run as correlated agent steps.",
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
        help="Expand event payloads inside every grouped step.",
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="Show the legacy flat event timeline with physical line numbers.",
    )
    parser.add_argument(
        "--html",
        type=Path,
        metavar="PATH",
        help="Write a self-contained offline HTML viewer.",
    )
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Render a trace report and return a process-style exit status."""

    args = build_parser().parse_args(argv)
    try:
        trace_path = args.trace.expanduser().resolve()
        if args.html is not None:
            if args.step is not None or args.verbose or args.events:
                raise TraceFormatError(
                    "--html cannot be combined with --step, --verbose, or --events"
                )
            output_path = args.html.expanduser().resolve()
            write_trace_html(trace_path, output_path)
            print(f"Wrote offline trace viewer: {output_path}")
            return 0
        report = render_trace_report(
            trace_path,
            step=args.step,
            verbose=args.verbose,
            events=args.events,
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
