from __future__ import annotations

import json
from pathlib import Path

import pytest

from yada.evals.cli import build_parser as build_eval_parser
from yada.run.cli import build_parser as build_run_parser
from yada.traces import (
    TRACE_SCHEMA_VERSION,
    TraceFormatError,
    TraceWriter,
    read_trace,
    reconstruct_model_request,
    render_trace_report,
)
from yada.traces.cli import run_cli


def test_trace_events_have_correlation_metadata_and_redaction(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    trace = TraceWriter(path, run_id="run-test")
    trace.write(
        "run_start",
        {
            "model": "fake",
            "task": "fix",
            "workspace": ".",
            "trace_level": "summary",
        },
    )
    trace.write(
        "assistant",
        {
            "step": 1,
            "duration_ms": 7,
            "message": {"reasoning_content": "private chain", "tool_calls": []},
            "usage": {"total_tokens": 12},
            "finish_reason": "stop",
        },
    )
    trace.write("run_end", {"finished": False, "steps": 1, "summary": "step limit"})

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert {event["run_id"] for event in events} == {"run-test"}
    assert {event["schema_version"] for event in events} == {TRACE_SCHEMA_VERSION}
    redacted = events[1]["data"]["message"]["reasoning_content"]
    assert redacted["redacted"] is True
    assert redacted["chars"] == len("private chain")
    assert "private chain" not in path.read_text()

    report = render_trace_report(path)
    assert "Run: run-test" in report
    assert "Trace level: summary" in report
    assert "Outcome: unfinished" in report
    assert "step=1 assistant duration=7ms" in report


def test_debug_trace_redacts_reasoning_and_common_secrets(tmp_path: Path) -> None:
    path = tmp_path / "debug.jsonl"
    trace = TraceWriter(path, level="debug", run_id="debug")
    trace.write(
        "model_request",
        {
            "step": 1,
            "payload": {
                "model": "fake",
                "api_key": "sk-secret-value",
                "messages": [
                    {
                        "role": "assistant",
                        "reasoning_content": "private reasoning",
                        "content": "Authorization: Bearer top-secret-token",
                    }
                ],
                "tools": [],
            },
        },
    )

    payload = reconstruct_model_request(read_trace(path), 1)

    assert payload["api_key"] == "[REDACTED]"
    assert payload["messages"][0]["reasoning_content"]["redacted"] is True
    assert payload["messages"][0]["content"] == "Authorization: Bearer [REDACTED]"
    stored = path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in stored
    assert "top-secret-token" not in stored
    assert "private reasoning" not in stored


def test_trace_reasoning_can_be_included_in_debug_payload(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    trace = TraceWriter(path, level="debug", include_reasoning=True)
    trace.write(
        "model_request",
        {
            "step": 2,
            "payload": {
                "messages": [
                    {"role": "assistant", "reasoning_content": "retained reasoning"}
                ]
            },
        },
    )

    payload = reconstruct_model_request(read_trace(path), 2)

    assert payload["messages"][0]["reasoning_content"] == "retained reasoning"


def test_step_and_verbose_reports_show_full_tool_details(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "details.jsonl"
    trace = TraceWriter(path, level="debug", run_id="details")
    trace.write(
        "run_start",
        {
            "model": "fake",
            "task": "debug",
            "workspace": ".",
            "trace_level": "debug",
        },
    )
    trace.write(
        "model_request",
        {
            "step": 1,
            "context": {"message_count": 2, "serialized_chars": 20},
            "payload": {
                "model": "fake",
                "messages": [{"role": "user", "content": "debug"}],
                "tools": [],
            },
        },
    )
    trace.write(
        "assistant",
        {
            "step": 1,
            "message": {"role": "assistant", "tool_calls": []},
            "duration_ms": 1,
            "finish_reason": "tool_calls",
        },
    )
    trace.write(
        "tool_call",
        {
            "step": 1,
            "tool_call_id": "call-1",
            "tool": "run_command",
            "arguments": {"argv": ["pytest", "-q"], "purpose": "test"},
        },
    )
    trace.write(
        "tool_result",
        {
            "step": 1,
            "tool_call_id": "call-1",
            "tool": "run_command",
            "duration_ms": 4,
            "result": {
                "ok": True,
                "argv": ["pytest", "-q"],
                "stdout": "1 failed",
                "stderr": "failure detail",
                "exit_code": 1,
            },
        },
    )
    trace.write("run_end", {"finished": False, "steps": 1, "summary": "failed"})

    summary = render_trace_report(path)
    detail = render_trace_report(path, step=1)
    verbose = render_trace_report(path, verbose=True)

    assert '"pytest"' not in summary
    assert '"pytest"' in detail
    assert '"stdout": "1 failed"' in detail
    assert '"stderr": "failure detail"' in detail
    assert '"messages"' in detail
    assert '"pytest"' in verbose
    assert run_cli([str(path), "--step", "1"]) == 0
    assert '"stdout": "1 failed"' in capsys.readouterr().out


def test_summary_trace_explains_missing_request_payload(tmp_path: Path) -> None:
    path = tmp_path / "summary.jsonl"
    trace = TraceWriter(path)
    trace.write("model_request", {"step": 1, "context": {"message_count": 2}})

    with pytest.raises(TraceFormatError, match="--trace-level debug"):
        reconstruct_model_request(read_trace(path), 1)

    detail = render_trace_report(path, step=1)
    assert "payload unavailable: rerun with --trace-level debug" in detail


def test_report_reads_legacy_schema_and_rejects_future_schema(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "run_start",
                "data": {"model": "old", "task": "fix", "workspace": "."},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert "Trace level: legacy" in render_trace_report(legacy)

    future = tmp_path / "future.jsonl"
    future.write_text(
        json.dumps(
            {
                "schema_version": TRACE_SCHEMA_VERSION + 1,
                "event": "run_start",
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TraceFormatError, match="newer than supported"):
        read_trace(future)


def test_report_marks_trace_without_run_end_as_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "crashed.jsonl"
    trace = TraceWriter(path, run_id="crashed")
    trace.write("run_start", {"model": "fake", "task": "fix", "workspace": "."})
    trace.write(
        "model_error",
        {"step": 1, "error_type": "TimeoutError", "error": "timed out"},
    )

    report = render_trace_report(path)

    assert "Outcome: interrupted (no run_end event)" in report
    assert "model_error TimeoutError: timed out" in report


def test_report_rejects_malformed_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"event": "run_start", "data": {}}\nnot-json\n')

    with pytest.raises(TraceFormatError, match="line 2"):
        render_trace_report(path)


def test_agent_clis_expose_trace_level() -> None:
    run_args = build_run_parser().parse_args(["fix", "--trace-level", "debug"])
    eval_args = build_eval_parser().parse_args(
        ["--case", "case-dir", "--trace-level", "debug"]
    )

    assert run_args.trace_level == "debug"
    assert eval_args.trace_level == "debug"
