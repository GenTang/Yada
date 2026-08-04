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
    build_trace_run,
    read_located_trace,
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
            "editing_strategy": "patch-only",
            "tool_names": ["read_file", "apply_patch", "finish_task"],
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
    assert "Editing strategy: patch-only" in report
    assert "Trace level: summary" in report
    assert "Outcome: unfinished" in report
    assert "Step 1/1 — fake  7ms  12 tokens  [L2]" in report
    assert "Model call  [request missing → response L2]" in report
    assert "Reasoning: 13 chars" in report


def test_debug_trace_includes_reasoning_and_redacts_common_secrets(
    tmp_path: Path,
) -> None:
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
                        "reasoning_content": (
                            "private reasoning; Authorization: Bearer reasoning-token"
                        ),
                        "content": "Authorization: Bearer top-secret-token",
                    }
                ],
                "tools": [],
            },
        },
    )

    payload = reconstruct_model_request(read_trace(path), 1)

    assert payload["api_key"] == "[REDACTED]"
    assert payload["messages"][0]["reasoning_content"] == (
        "private reasoning; Authorization: Bearer [REDACTED]"
    )
    assert payload["messages"][0]["content"] == "Authorization: Bearer [REDACTED]"
    stored = path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in stored
    assert "top-secret-token" not in stored
    assert "reasoning-token" not in stored
    assert "private reasoning" in stored


def test_debug_trace_always_includes_reasoning(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    trace = TraceWriter(path, level="debug")
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
    assert "Model call  [request missing → error L2]" in report
    assert "Error: TimeoutError: timed out" in report


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
    assert "--trace-reasoning" not in build_run_parser().format_help()
    assert "--trace-reasoning" not in build_eval_parser().format_help()


def test_grouped_report_preserves_lines_and_multi_tool_order(tmp_path: Path) -> None:
    path = tmp_path / "multi-tool.jsonl"
    records = [
        _record(
            1,
            "run_start",
            {
                "model": "deepseek-v4-pro",
                "task": "fix parser",
                "max_steps": 30,
                "trace_level": "summary",
            },
        ),
        _record(2, "model_request", {"step": 1, "request_id": "request-1"}),
        _record(
            3,
            "assistant",
            {
                "step": 1,
                "request_id": "request-1",
                "duration_ms": 1800,
                "finish_reason": "tool_calls",
                "usage": {"total_tokens": 1246},
                "message": {
                    "reasoning_content": {"redacted": True, "chars": 381},
                    "tool_calls": [],
                },
            },
        ),
        _record(4, "plan_decision", {"step": 1, "action": "execute_tools"}),
        _record(
            5,
            "tool_call",
            {"step": 1, "tool_call_id": "search", "tool": "search_code"},
        ),
        _record(
            6,
            "tool_call",
            {"step": 1, "tool_call_id": "command", "tool": "run_command"},
        ),
        _record(
            7,
            "tool_result",
            {
                "step": 1,
                "tool_call_id": "command",
                "tool": "run_command",
                "duration_ms": 37,
                "result": {"ok": True, "exit_code": 1},
            },
        ),
        _record(
            8,
            "tool_result",
            {
                "step": 1,
                "tool_call_id": "search",
                "tool": "search_code",
                "duration_ms": 42,
                "result": {"ok": True},
            },
        ),
        _record(9, "run_end", {"finished": True, "steps": 1}),
    ]
    path.write_text(
        json.dumps(records[0])
        + "\n\n"
        + "\n".join(json.dumps(record) for record in records[1:])
        + "\n",
        encoding="utf-8",
    )

    located = read_located_trace(path)
    run = build_trace_run(located)
    report = render_trace_report(path)

    assert [event.line_number for event in located[:3]] == [1, 3, 4]
    assert "line_number" not in read_trace(path)[1]
    assert run.steps[0].first_line == 3
    assert run.steps[0].last_line == 9
    assert "Step 1/30 — deepseek-v4-pro  1.8s  1,246 tokens  [L3–L9]" in report
    assert "Model call  [request L3 → response L4]" in report
    assert "Finish reason: tool_calls" in report
    assert "Reasoning: 381 chars" in report
    assert "Plan: execute_tools  [L5]" in report
    search = "[ok] search_code  42ms  [call L6 → result L9]"
    command = "[ok] run_command  37ms  exit=1  [call L7 → result L8]"
    assert search in report
    assert command in report
    assert report.index(search) < report.index(command)


def test_report_marks_missing_model_response_and_tool_result(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.jsonl"
    records = [
        _record(1, "run_start", {"model": "fake", "max_steps": 30}),
        _record(2, "model_request", {"step": 7, "request_id": "request-7"}),
        _record(
            3,
            "tool_call",
            {"step": 7, "tool_call_id": "orphan", "tool": "read_file"},
        ),
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    report = render_trace_report(path)

    assert "Step 7/30 — interrupted  [L2–L3]" in report
    assert "Model call  [request L2 → response missing]" in report
    assert "[missing] read_file  [call L3 → result missing]" in report


def test_protocol_violation_and_failed_tool_keep_line_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "violation.jsonl"
    records = [
        _record(1, "model_request", {"step": 2, "request_id": "request-2"}),
        _record(
            2,
            "assistant",
            {
                "step": 2,
                "request_id": "request-2",
                "duration_ms": 5,
                "finish_reason": "tool_calls",
                "message": {"reasoning_content": "bad call"},
            },
        ),
        _record(
            3,
            "plan_decision",
            {
                "step": 2,
                "action": "execute_tools",
                "rejection_error": "duplicate finish_task calls",
            },
        ),
        _record(
            4,
            "protocol_violation",
            {"step": 2, "error": "duplicate finish_task calls"},
        ),
        _record(
            5,
            "tool_call",
            {"step": 2, "tool_call_id": "bad", "tool": "finish_task"},
        ),
        _record(
            6,
            "tool_result",
            {
                "step": 2,
                "tool_call_id": "bad",
                "tool": "finish_task",
                "duration_ms": 0,
                "result": {"ok": False, "error": "duplicate finish_task calls"},
            },
        ),
        _record(
            7,
            "protocol_reminder",
            {"step": 2, "text": "use the required tool protocol"},
        ),
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    report = render_trace_report(path)

    assert "Plan: execute_tools  [L3]" in report
    assert "Protocol violation: duplicate finish_task calls  [L4]" in report
    assert "Protocol reminder: use the required tool protocol  [L7]" in report
    assert "[error] finish_task  0ms  [call L5 → result L6]" in report


def test_step_verbose_events_and_flat_event_mode_use_physical_lines(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "expanded.jsonl"
    path.write_text(
        json.dumps(_record(10, "model_request", {"step": 3, "request_id": "r"}))
        + "\n\n"
        + json.dumps(
            _record(
                11,
                "assistant",
                {
                    "request_id": "r",
                    "finish_reason": "stop",
                    "message": {"content": "done"},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    detail = render_trace_report(path, step=3)
    flat = render_trace_report(path, events=True)

    assert "L1 model_request  sequence=10" in detail
    assert "L3 assistant  sequence=11" in detail
    assert "Model call  [request L1 → response L3]" in detail
    assert "L1 [ 10" in flat
    assert "L3 [ 11" in flat
    assert run_cli([str(path), "--events"]) == 0
    assert "L3 [ 11" in capsys.readouterr().out


def _record(sequence: int, event: str, data: dict) -> dict:
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": "test-run",
        "sequence": sequence,
        "elapsed_ms": sequence,
        "event": event,
        "data": data,
    }
