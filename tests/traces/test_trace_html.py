from __future__ import annotations

import json
from pathlib import Path

from yada.traces import TRACE_SCHEMA_VERSION, TraceWriter, render_trace_html
from yada.traces.cli import run_cli


def test_completed_trace_renders_offline_semantic_view(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    trace_path = tmp_path / "completed.jsonl"
    output_path = tmp_path / "viewer.html"
    opened = []
    monkeypatch.setattr(
        "yada.traces.cli.webbrowser.open",
        lambda url, new: opened.append((url, new)) or True,
    )
    trace = TraceWriter(trace_path, level="debug", run_id="html-test")
    trace.write(
        "run_start",
        {
            "model": "deepseek-v4-pro",
            "task": "Fix parser\n\nA long issue description that stays collapsed.",
            "trace_level": "debug",
            "model_config": {"thinking": True},
            "provenance": {"yada_version": "0.1.0", "case_id": "parser-1"},
        },
    )
    trace.write(
        "model_request",
        {
            "step": 1,
            "request_id": "request-1",
            "payload": {"messages": [{"role": "user", "content": "fix it"}]},
        },
    )
    trace.write(
        "assistant",
        {
            "step": 1,
            "request_id": "request-1",
            "duration_ms": 1250,
            "usage": {"total_tokens": 42},
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": "The boundary is off by one.",
                "tool_calls": [],
            },
        },
    )
    trace.write(
        "plan_decision",
        {"step": 1, "action": "execute_tools", "tools": ["apply_patch"]},
    )
    trace.write(
        "tool_call",
        {
            "step": 1,
            "tool_call_id": "patch-1",
            "tool": "apply_patch",
            "arguments": {"patch": "diff --git a/a.py b/a.py"},
        },
    )
    trace.write(
        "tool_result",
        {
            "step": 1,
            "tool_call_id": "patch-1",
            "tool": "apply_patch",
            "duration_ms": 4,
            "result": {
                "ok": True,
                "changed_files": [{"path": "a.py", "sha256": "abc"}],
            },
        },
    )
    trace.write(
        "run_end",
        {
            "finished": True,
            "steps": 1,
            "usage": {"total_tokens": 42},
            "final_state": {
                "git_status": " M a.py",
                "diff_stat": "a.py | 2 +-",
                "diff": "-wrong\n+right",
            },
        },
    )

    assert run_cli([str(trace_path), "--html", str(output_path)]) == 0
    document = output_path.read_text(encoding="utf-8")

    assert "Wrote offline trace viewer" in capsys.readouterr().out
    assert "Yada trace viewer · offline" in document
    assert "<h1>Fix parser</h1>" in document
    assert (
        '<details class="trace-section task-detail"><summary>Task</summary>' in document
    )
    assert "A long issue description that stays collapsed." in document
    assert "Resolved · 1 step" in document
    assert "The boundary is off by one." in document
    assert "<summary>Reasoning</summary>" not in document
    assert '"reasoning_content"' not in document
    assert "&quot;reasoning_content&quot;" in document
    assert "message_field_presence" not in document
    assert "Final diff" in document
    assert "-wrong\n+right" in document
    assert 'data-flags="file-change"' in document
    assert "<script src=" not in document
    assert "<link " not in document
    assert "fetch(" not in document
    assert "XMLHttpRequest" not in document
    assert "default-src &#x27;none&#x27;" not in document
    assert "default-src 'none'" in document
    assert opened == []
    assert 'id="search-status"' in document
    assert '<label for="step-filter">Filter steps</label>' in document
    assert "const searchableText = new Map(" in document
    assert 'panel.querySelectorAll("[data-searchable]")' in document
    assert '.join(" ")' in document
    assert '<details class="trace-section"><summary>Request</summary>' in document
    assert (
        '<details class="trace-section" data-searchable open>'
        "<summary>Response</summary>" in document
    )
    assert "applyFilters();" in document


def test_html_without_path_writes_beside_trace_and_opens(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    trace_path = tmp_path / "nested" / "run.jsonl"
    trace = TraceWriter(trace_path, level="summary", run_id="auto-html")
    trace.write("run_start", {"model": "fake", "task": "open trace"})
    opened = []
    monkeypatch.setattr(
        "yada.traces.cli.webbrowser.open",
        lambda url, new: opened.append((url, new)) or True,
    )

    assert run_cli([str(trace_path), "--html"]) == 0

    output_path = trace_path.with_suffix(".html").resolve()
    assert output_path.is_file()
    assert opened == [(output_path.as_uri(), 2)]
    assert "Wrote and opened offline trace viewer" in capsys.readouterr().out


def test_interrupted_trace_marks_missing_pairs_and_run_end(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                _record(1, "run_start", {"model": "fake", "task": "crash"}),
                _record(
                    2,
                    "model_request",
                    {"step": 7, "request_id": "request-7"},
                ),
                _record(
                    3,
                    "tool_call",
                    {"step": 7, "tool_call_id": "orphan", "tool": "read_file"},
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    document = render_trace_html(path)

    assert "Interrupted trace: missing run_end" in document
    assert "Missing model response" in document
    assert "Missing tool result" in document
    assert 'data-flags="incomplete"' in document


def test_legacy_trace_ignores_obsolete_field_presence(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jsonl"
    records = [
        {"schema_version": 1, "event": "run_start", "data": {"model": "old"}},
        {
            "schema_version": 1,
            "event": "assistant",
            "data": {
                "step": 1,
                "message_field_presence": {"content": True},
                "message": {"content": "done"},
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    document = render_trace_html(path)

    assert "run legacy" in document
    assert "message_field_presence" not in document
    assert "done" in document


def test_large_tool_output_is_collapsed_by_default(tmp_path: Path) -> None:
    path = tmp_path / "large.jsonl"
    records = [
        _record(1, "model_request", {"step": 1}),
        _record(2, "assistant", {"step": 1, "message": {"content": "run"}}),
        _record(
            3,
            "tool_call",
            {"step": 1, "tool_call_id": "command", "tool": "run_command"},
        ),
        _record(
            4,
            "tool_result",
            {
                "step": 1,
                "tool_call_id": "command",
                "tool": "run_command",
                "result": {"ok": True, "exit_code": 0, "stdout": "x" * 6_000},
            },
        ),
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    document = render_trace_html(path)

    marker = (
        '<details class="trace-section" data-searchable><summary>Tool Results</summary>'
    )
    assert marker in document
    assert "x" * 6_000 in document


def test_malicious_html_is_escaped_and_output_cannot_replace_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malicious.jsonl"
    attack = (
        "</script><script>globalThis.pwned = true</script><img src=x onerror=alert(1)>"
    )
    trace = TraceWriter(path, level="debug")
    trace.write("run_start", {"task": attack, "model": "fake"})
    trace.write(
        "assistant",
        {"step": 1, "message": {"content": attack, "reasoning_content": attack}},
    )

    document = render_trace_html(path)

    assert attack not in document
    assert "&lt;/script&gt;&lt;script&gt;" in document
    assert "<img src=x" not in document
    assert run_cli([str(path), "--html", str(path)]) == 2
    assert path.read_text(encoding="utf-8").startswith("{")


def _record(sequence: int, event: str, data: dict) -> dict:
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": "html-test",
        "sequence": sequence,
        "elapsed_ms": sequence,
        "event": event,
        "data": data,
    }
