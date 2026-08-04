"""Self-contained offline HTML rendering for Yada traces."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from yada.traces.report import (
    LocatedTraceEvent,
    TraceFormatError,
    TraceRun,
    TraceStep,
    TraceToolExecution,
    build_trace_run,
    read_located_trace,
)

_FIELDS = ("role", "content", "reasoning_content", "tool_calls")
_EDIT_TOOLS = {"apply_patch", "replace_text"}
_COLLAPSE_CHARS = 4_000


def render_trace_html(path: Path) -> str:
    """Render one validated JSONL trace as a portable offline HTML document."""

    run = build_trace_run(read_located_trace(path))
    start = run.run_start.data if run.run_start else {}
    run_id = _run_id(run)
    panels = "".join(
        _render_step(step, selected=index == 0) for index, step in enumerate(run.steps)
    )
    navigation = "".join(
        _render_step_button(step, selected=index == 0)
        for index, step in enumerate(run.steps)
    )
    if not run.steps:
        panels = '<p class="empty">No agent steps were recorded.</p>'

    content = (
        _render_header(path, run)
        + _render_run_details(run)
        + '<div class="workspace">'
        + '<aside class="sidebar">'
        + '<label for="trace-search">Search this trace</label>'
        + '<input id="trace-search" type="search" '
        + 'placeholder="Prompt, tool, file, error…">'
        + '<p id="search-status" class="search-status" aria-live="polite"></p>'
        + _render_filters()
        + f'<nav id="step-list" aria-label="Agent steps">{navigation}</nav>'
        + '<p id="no-results" class="empty" hidden>No matching steps.</p>'
        + "</aside>"
        + f'<section id="step-panels">{panels}</section>'
        + "</div>"
    )
    title = f"Yada trace · {run_id} · {start.get('model', 'unknown')}"
    return _DOCUMENT.replace("__TITLE__", _escape(title), 1).replace(
        "__CONTENT__", content, 1
    )


def write_trace_html(trace_path: Path, output_path: Path) -> None:
    """Validate ``trace_path`` and write its self-contained HTML view."""

    trace_path = trace_path.resolve()
    output_path = output_path.resolve()
    if trace_path == output_path:
        raise TraceFormatError("--html output must differ from the source trace")
    document = render_trace_html(trace_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")


def _render_header(path: Path, run: TraceRun) -> str:
    start = run.run_start.data if run.run_start else {}
    outcome, outcome_class = _outcome(run)
    token_count = _run_tokens(run)
    latency = _last_elapsed(run)
    interrupted = ""
    if run.run_end is None:
        interrupted = (
            '<p class="notice error">Interrupted trace: missing run_end. '
            "The final state may be unavailable.</p>"
        )
    return f"""
<header class="run-header">
  <div>
    <p class="eyebrow">Yada trace viewer · offline</p>
    <h1>{_escape(start.get("task", "Untitled run"))}</h1>
    <p class="muted">{_escape(path)} · run {_escape(_run_id(run))}</p>
  </div>
  <div class="metrics">
    {_metric("Outcome", outcome, outcome_class)}
    {_metric("Model", start.get("model", "unknown"))}
    {_metric("Tokens", f"{token_count:,}" if token_count is not None else "—")}
    {_metric("Latency", _duration(latency) if latency is not None else "—")}
    {_metric("Steps", len(run.steps))}
  </div>
</header>
{interrupted}
"""


def _render_run_details(run: TraceRun) -> str:
    start = run.run_start.data if run.run_start else {}
    end = run.run_end.data if run.run_end else {}
    details = '<section class="run-details">'
    details += _details(
        "Model configuration",
        start.get("model_config", "Unavailable"),
        open_by_default=True,
    )
    details += _details(
        "Provenance",
        start.get("provenance", "Unavailable"),
        open_by_default=True,
    )
    final_state = end.get("final_state")
    if isinstance(final_state, dict):
        details += _details(
            "Final status",
            {
                "git_status": final_state.get("git_status"),
                "diff_stat": final_state.get("diff_stat"),
            },
            open_by_default=True,
        )
        details += _details(
            "Final diff",
            final_state.get("diff") or "No diff recorded.",
            open_by_default=False,
        )
    else:
        details += _details(
            "Final diff",
            "Unavailable: the trace has no captured final state.",
            open_by_default=True,
        )
    return details + "</section>"


def _render_filters() -> str:
    filters = (
        ("model-error", "Model errors"),
        ("protocol-violation", "Protocol violations"),
        ("rejected-tool", "Rejected tools"),
        ("nonzero-exit", "Non-zero exits"),
        ("file-change", "File-changing steps"),
    )
    controls = "".join(
        f'<label class="filter"><input type="checkbox" value="{value}"> {label}</label>'
        for value, label in filters
    )
    return f'<fieldset id="filters"><legend>Filters</legend>{controls}</fieldset>'


def _render_step_button(step: TraceStep, *, selected: bool) -> str:
    flags = _step_flags(step)
    response = step.model_response
    duration = response.data.get("duration_ms") if response else None
    tokens = _total_tokens(response.data.get("usage")) if response else None
    facts = []
    if isinstance(duration, (int, float)):
        facts.append(_duration(duration))
    if tokens is not None:
        facts.append(f"{tokens:,} tokens")
    if not facts:
        facts.append("interrupted" if _step_interrupted(step) else "no metrics")
    classes = "step-link selected" if selected else "step-link"
    flag_text = " ".join(flags)
    return (
        f'<button class="{classes}" type="button" data-step="{step.number}" '
        f'data-flags="{flag_text}" aria-selected="{str(selected).lower()}">'
        f"<strong>Step {step.number}</strong>"
        f"<span>{_escape(' · '.join(facts))}</span></button>"
    )


def _render_step(step: TraceStep, *, selected: bool) -> str:
    flags = _step_flags(step)
    hidden = "" if selected else " hidden"
    flag_text = " ".join(flags)
    badges = "".join(f'<span class="badge">{_escape(flag)}</span>' for flag in flags)
    warnings = _render_step_warnings(step)
    request = _located(step.model_request)
    response = _response_without_reasoning(step.model_response)
    plan = _located(step.plan_decision)
    calls = [_located(execution.call) for execution in step.tool_executions]
    results = [_located(execution.result) for execution in step.tool_executions]
    return f"""
<article class="step-panel" id="step-{step.number}" data-flags="{flag_text}"{hidden}>
  <header class="step-header">
    <div><p class="eyebrow">Lines {step.first_line}–{step.last_line}</p>
    <h2>Step {step.number}</h2></div>
    <div class="badges">{badges}</div>
  </header>
  {warnings}
  {_details("Request", request or "Missing model request", open_by_default=False)}
  {_render_reasoning(step.model_response)}
  {_render_presence(step.model_response)}
  {_details("Response", response or "Missing model response", open_by_default=True)}
  {_details("Plan", plan or "No plan decision recorded", open_by_default=True)}
  {_details("Tool Calls", calls or "No tool calls", open_by_default=True)}
  {_details("Tool Results", results or "No tool results", open_by_default=True)}
</article>
"""


def _render_reasoning(response: LocatedTraceEvent | None) -> str:
    if response is None or response.name != "assistant":
        return _details(
            "Reasoning", "Unavailable: no assistant response.", open_by_default=True
        )
    message = response.data.get("message")
    if not isinstance(message, dict) or "reasoning_content" not in message:
        return _details("Reasoning", "Omitted by the model.", open_by_default=True)
    reasoning = message["reasoning_content"]
    if isinstance(reasoning, dict) and reasoning.get("redacted") is True:
        chars = reasoning.get("chars", "unknown")
        note = f"Redacted in the source trace ({chars} characters)."
        return (
            '<details class="trace-section" open><summary>Reasoning</summary>'
            f'<p class="notice">{_escape(note)}</p><pre>{_json(reasoning)}</pre>'
            "</details>"
        )
    return _details("Reasoning", reasoning, open_by_default=True)


def _render_presence(response: LocatedTraceEvent | None) -> str:
    if response is None or response.name != "assistant":
        return ""
    presence = response.data.get("message_field_presence")
    if not isinstance(presence, dict):
        return (
            '<section class="presence"><h3>Message field presence</h3>'
            '<p class="muted">Unavailable for this legacy trace.</p></section>'
        )
    fields = []
    for field in _FIELDS:
        value = presence.get(field)
        state = (
            "present" if value is True else "omitted" if value is False else "unknown"
        )
        fields.append(
            f'<span class="presence-item"><code>{field}</code> {state}</span>'
        )
    return (
        '<section class="presence"><h3>Message field presence</h3>'
        f'<div class="presence-list">{"".join(fields)}</div></section>'
    )


def _render_step_warnings(step: TraceStep) -> str:
    warnings = []
    if step.model_request is None:
        warnings.append("Missing model request")
    if step.model_request is not None and step.model_response is None:
        warnings.append("Missing model response")
    for execution in step.tool_executions:
        if execution.call is None:
            warnings.append("Unmatched tool result")
        if execution.result is None:
            warnings.append("Missing tool result")
    if not warnings:
        return ""
    unique = " · ".join(dict.fromkeys(warnings))
    return f'<p class="notice error">{_escape(unique)}</p>'


def _step_flags(step: TraceStep) -> tuple[str, ...]:
    flags = []
    if step.model_response and step.model_response.name == "model_error":
        flags.append("model-error")
    if any(event.name == "protocol_violation" for event in step.protocol_events):
        flags.append("protocol-violation")
    if any(
        execution.call and execution.call.data.get("rejected") is True
        for execution in step.tool_executions
    ):
        flags.append("rejected-tool")
    if any(_nonzero_exit(execution) for execution in step.tool_executions):
        flags.append("nonzero-exit")
    if any(_changes_files(execution) for execution in step.tool_executions):
        flags.append("file-change")
    if _step_interrupted(step):
        flags.append("incomplete")
    return tuple(flags)


def _step_interrupted(step: TraceStep) -> bool:
    return bool(
        (step.model_request and step.model_response is None)
        or any(
            execution.call is None or execution.result is None
            for execution in step.tool_executions
        )
    )


def _nonzero_exit(execution: TraceToolExecution) -> bool:
    result = _tool_result(execution)
    exit_code = result.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def _changes_files(execution: TraceToolExecution) -> bool:
    result = _tool_result(execution)
    if result.get("ok") is not True:
        return False
    changed = result.get("changed_files")
    if isinstance(changed, list) and changed:
        return True
    source = execution.call or execution.result
    return bool(source and source.data.get("tool") in _EDIT_TOOLS)


def _tool_result(execution: TraceToolExecution) -> dict[str, Any]:
    if execution.result is None:
        return {}
    result = execution.result.data.get("result")
    return result if isinstance(result, dict) else {}


def _response_without_reasoning(
    response: LocatedTraceEvent | None,
) -> dict[str, Any] | None:
    located = _located(response)
    if located is None or response is None or response.name != "assistant":
        return located
    data = dict(response.data)
    message = data.get("message")
    if isinstance(message, dict):
        data["message"] = {
            key: value for key, value in message.items() if key != "reasoning_content"
        }
    return {"jsonl_line": response.line_number, "event": response.name, "data": data}


def _located(event: LocatedTraceEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "jsonl_line": event.line_number,
        "event": event.name,
        "data": event.data,
    }


def _details(title: str, value: Any, *, open_by_default: bool) -> str:
    rendered = _json_text(value)
    open_attribute = (
        " open" if open_by_default and len(rendered) <= _COLLAPSE_CHARS else ""
    )
    return (
        f'<details class="trace-section"{open_attribute}>'
        f"<summary>{_escape(title)}</summary><pre>{_escape(rendered)}</pre></details>"
    )


def _json(value: Any) -> str:
    return _escape(_json_text(value))


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )


def _metric(label: str, value: Any, class_name: str = "") -> str:
    css_class = f' class="{class_name}"' if class_name else ""
    return (
        '<div class="metric">'
        f"<span>{_escape(label)}</span><strong{css_class}>{_escape(value)}</strong>"
        "</div>"
    )


def _outcome(run: TraceRun) -> tuple[str, str]:
    if run.run_end is None:
        return "Interrupted", "error-text"
    steps = run.run_end.data.get("steps")
    suffix = ""
    if isinstance(steps, int):
        suffix = f" · {steps} {'step' if steps == 1 else 'steps'}"
    if run.run_end.data.get("finished") is True:
        return f"Resolved{suffix}", "success-text"
    return f"Unfinished{suffix}", "error-text"


def _run_id(run: TraceRun) -> str:
    return str(
        next(
            (
                event.record.get("run_id")
                for event in run.events
                if event.record.get("run_id")
            ),
            "legacy",
        )
    )


def _run_tokens(run: TraceRun) -> int | None:
    if run.run_end:
        total = _total_tokens(run.run_end.data.get("usage"))
        if total is not None:
            return total
    totals = [
        _total_tokens(event.data.get("usage"))
        for event in run.events
        if event.name == "assistant"
    ]
    values = [value for value in totals if value is not None]
    return sum(values) if values else None


def _total_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt + completion
    return None


def _last_elapsed(run: TraceRun) -> int | float | None:
    values = [
        event.record.get("elapsed_ms")
        for event in run.events
        if isinstance(event.record.get("elapsed_ms"), (int, float))
    ]
    return max(values) if values else None


def _duration(milliseconds: int | float) -> str:
    if milliseconds < 1000:
        return f"{milliseconds:g} ms"
    seconds = milliseconds / 1000
    return f"{seconds:.1f} s" if seconds < 10 else f"{seconds:g} s"


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>__TITLE__</title>
<style>
:root { color-scheme: light dark; --bg:#f6f7f9; --panel:#fff; --text:#172033;
  --muted:#647084; --border:#dbe0e8; --accent:#3157d5; --accent-soft:#e8edff;
  --error:#b42318; --success:#08783e; --code:#f2f4f7; }
@media (prefers-color-scheme: dark) { :root { --bg:#0e1117; --panel:#171b23;
  --text:#e7eaf0; --muted:#9aa5b5; --border:#303744; --accent:#91a7ff;
  --accent-soft:#252f52; --error:#ff8b84; --success:#67d59a; --code:#11151c; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5
  ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1440px; margin:auto; padding:28px; }
h1,h2,h3,p { margin-top:0; } h1 { margin-bottom:6px; font-size:24px; }
h2 { margin:0; font-size:20px; } h3 { margin-bottom:8px; font-size:14px; }
.eyebrow { margin-bottom:5px; color:var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:.08em; } .muted { color:var(--muted); }
.run-header { display:flex; gap:24px; justify-content:space-between; align-items:flex-start; }
.metrics { display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }
.metric { min-width:112px; padding:10px 12px; background:var(--panel);
  border:1px solid var(--border); border-radius:8px; }
.metric span { display:block; color:var(--muted); font-size:12px; }
.metric strong { display:block; margin-top:2px; font-weight:600; }
.success-text { color:var(--success); } .error-text { color:var(--error); }
.notice { margin:14px 0 0; padding:10px 12px; background:var(--accent-soft);
  border-left:3px solid var(--accent); border-radius:4px; }
.notice.error { border-color:var(--error); }
.run-details { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px;
  margin:18px 0; }
.workspace { display:grid; grid-template-columns:280px minmax(0,1fr); gap:18px;
  align-items:start; }
.sidebar,.step-panel { background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:16px; }
.sidebar { position:sticky; top:16px; max-height:calc(100vh - 32px); overflow:auto; }
.sidebar > label, legend { font-weight:600; }
input[type="search"] { width:100%; margin:7px 0 5px; padding:9px 10px;
  color:var(--text); background:var(--bg); border:1px solid var(--border); border-radius:6px; }
.search-status { margin:0 0 14px; color:var(--muted); font-size:12px; }
fieldset { margin:0 0 14px; padding:0; border:0; }
.filter { display:block; margin:7px 0; color:var(--muted); }
#step-list { display:grid; gap:6px; }
.step-link { width:100%; padding:9px 10px; text-align:left; color:var(--text);
  background:transparent; border:1px solid var(--border); border-radius:6px; cursor:pointer; }
.step-link span { display:block; color:var(--muted); font-size:12px; }
.step-link:hover,.step-link.selected { border-color:var(--accent); background:var(--accent-soft); }
.step-header { display:flex; align-items:flex-start; justify-content:space-between; gap:14px;
  padding-bottom:12px; border-bottom:1px solid var(--border); }
.badges { display:flex; flex-wrap:wrap; gap:5px; justify-content:flex-end; }
.badge,.presence-item { padding:3px 7px; background:var(--accent-soft); border-radius:999px;
  color:var(--text); font-size:12px; }
.trace-section { margin-top:12px; border-top:1px solid var(--border); padding-top:10px; }
.trace-section summary { cursor:pointer; font-weight:600; }
pre { max-height:520px; overflow:auto; margin:9px 0 0; padding:12px; background:var(--code);
  border-radius:6px; color:var(--text); white-space:pre-wrap; overflow-wrap:anywhere;
  font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
.presence { margin-top:12px; padding-top:10px; border-top:1px solid var(--border); }
.presence-list { display:flex; flex-wrap:wrap; gap:6px; }
.empty { color:var(--muted); text-align:center; padding:18px 4px; }
[hidden] { display:none !important; }
@media (max-width:900px) { main { padding:16px; } .run-header { display:block; }
  .metrics { justify-content:flex-start; margin-top:14px; }
  .run-details { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .workspace { grid-template-columns:1fr; } .sidebar { position:static; max-height:none; } }
@media (max-width:520px) { .run-details { grid-template-columns:1fr; }
  .metric { flex:1 1 120px; } }
</style>
</head>
<body><main>__CONTENT__</main>
<script>
(() => {
  const search = document.getElementById("trace-search");
  const filters = [...document.querySelectorAll("#filters input")];
  const buttons = [...document.querySelectorAll(".step-link")];
  const panels = new Map(
    [...document.querySelectorAll(".step-panel")].map(panel => [panel.id.slice(5), panel])
  );
  const searchableText = new Map(
    [...panels].map(([step, panel]) => [step, panel.textContent.toLocaleLowerCase()])
  );
  const searchStatus = document.getElementById("search-status");
  const noResults = document.getElementById("no-results");

  function select(button) {
    buttons.forEach(item => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-selected", String(selected));
      const panel = panels.get(item.dataset.step);
      if (panel) panel.hidden = !selected;
    });
  }

  function applyFilters() {
    const query = search.value.trim().toLocaleLowerCase();
    const active = filters.filter(item => item.checked).map(item => item.value);
    const visible = [];
    buttons.forEach(button => {
      const flags = new Set((button.dataset.flags || "").split(" ").filter(Boolean));
      const matchesFilter = active.length === 0 || active.some(flag => flags.has(flag));
      const matchesSearch = !query || (searchableText.get(button.dataset.step) || "").includes(query);
      button.hidden = !(matchesFilter && matchesSearch);
      if (!button.hidden) visible.push(button);
    });
    const totalLabel = buttons.length === 1 ? "step" : "steps";
    searchStatus.textContent = visible.length === buttons.length
      ? `${buttons.length} ${totalLabel}`
      : `${visible.length} of ${buttons.length} ${totalLabel}`;
    noResults.hidden = (!query && active.length === 0) || visible.length !== 0;
    const selected = buttons.find(item => item.getAttribute("aria-selected") === "true");
    if (!selected || selected.hidden) {
      if (visible[0]) select(visible[0]);
      else panels.forEach(panel => { panel.hidden = true; });
    }
  }

  buttons.forEach(button => button.addEventListener("click", () => select(button)));
  search.addEventListener("input", applyFilters);
  filters.forEach(filter => filter.addEventListener("change", applyFilters));
  applyFilters();
})();
</script>
</body>
</html>
"""
