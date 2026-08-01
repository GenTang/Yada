from __future__ import annotations

import json
from pathlib import Path

import pytest

from yada.traces import TraceFormatError, TraceWriter, render_trace_report


def test_trace_events_have_correlation_metadata_and_redaction(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    trace = TraceWriter(path, run_id="run-test")
    trace.write("run_start", {"model": "fake", "task": "fix", "workspace": "."})
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
    trace.write(
        "run_end", {"finished": False, "steps": 1, "summary": "step limit"}
    )

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert {event["run_id"] for event in events} == {"run-test"}
    assert {event["schema_version"] for event in events} == {1}
    redacted = events[1]["data"]["message"]["reasoning_content"]
    assert redacted["redacted"] is True
    assert redacted["chars"] == len("private chain")
    assert "private chain" not in path.read_text()

    report = render_trace_report(path)
    assert "Run: run-test" in report
    assert "Outcome: unfinished" in report
    assert "step=1 assistant duration=7ms" in report


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
