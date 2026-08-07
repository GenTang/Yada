"""Step-oriented diagnostics derived from Yada JSONL traces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from yada.traces.jsonl import TRACE_SCHEMA_VERSION


class TraceFormatError(ValueError):
    """Raised when a JSONL trace contains an invalid event record."""


@dataclass(frozen=True)
class LocatedTraceEvent:
    """One trace event paired with its physical JSONL source line."""

    line_number: int
    record: dict[str, Any]

    @property
    def name(self) -> str:
        """Return the event type."""

        return self.record["event"]

    @property
    def data(self) -> dict[str, Any]:
        """Return the event-specific payload."""

        return self.record["data"]


@dataclass(frozen=True)
class TraceToolExecution:
    """A tool call and its correlated result, either of which may be missing."""

    call: LocatedTraceEvent | None
    result: LocatedTraceEvent | None


@dataclass(frozen=True)
class TraceStep:
    """All located events that belong to one agent-loop step."""

    number: int
    events: tuple[LocatedTraceEvent, ...]
    model_request: LocatedTraceEvent | None
    model_response: LocatedTraceEvent | None
    plan_decision: LocatedTraceEvent | None
    protocol_events: tuple[LocatedTraceEvent, ...]
    tool_executions: tuple[TraceToolExecution, ...]

    @property
    def first_line(self) -> int:
        """Return the first physical source line occupied by this step."""

        return min(event.line_number for event in self.events)

    @property
    def last_line(self) -> int:
        """Return the last physical source line occupied by this step."""

        return max(event.line_number for event in self.events)


@dataclass(frozen=True)
class TraceRun:
    """Normalized, source-located reporting model for one trace run."""

    events: tuple[LocatedTraceEvent, ...]
    run_start: LocatedTraceEvent | None
    steps: tuple[TraceStep, ...]
    run_end: LocatedTraceEvent | None
    ungrouped_events: tuple[LocatedTraceEvent, ...]


def read_located_trace(path: Path) -> list[LocatedTraceEvent]:
    """Load trace events while retaining their physical JSONL line numbers."""

    events: list[LocatedTraceEvent] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceFormatError(
                f"line {line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            raise TraceFormatError(
                f"line {line_number}: expected an object with a string event field"
            )
        if not isinstance(record.get("data"), dict):
            raise TraceFormatError(f"line {line_number}: data must be an object")
        schema_version = record.get("schema_version")
        if isinstance(schema_version, int) and schema_version > TRACE_SCHEMA_VERSION:
            raise TraceFormatError(
                f"line {line_number}: trace schema {schema_version} is newer than "
                f"supported schema {TRACE_SCHEMA_VERSION}"
            )
        events.append(LocatedTraceEvent(line_number, record))
    if not events:
        raise TraceFormatError("trace contains no events")
    return events


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Load trace records in file order without changing the public API."""

    return [event.record for event in read_located_trace(path)]


def build_trace_run(events: Iterable[LocatedTraceEvent]) -> TraceRun:
    """Correlate located events into a reusable step-oriented reporting model."""

    located = tuple(sorted(events, key=_event_order))
    if not located:
        raise TraceFormatError("trace contains no events")

    request_steps: dict[str, int] = {}
    tool_steps: dict[str, int] = {}
    for event in located:
        step = _step_number(event.data.get("step"))
        if step is None:
            continue
        request_id = event.data.get("request_id")
        tool_call_id = event.data.get("tool_call_id")
        if request_id is not None:
            request_steps[str(request_id)] = step
        if tool_call_id is not None:
            tool_steps[str(tool_call_id)] = step

    step_events: dict[int, list[LocatedTraceEvent]] = {}
    ungrouped: list[LocatedTraceEvent] = []
    for event in located:
        step = _step_number(event.data.get("step"))
        if step is None and event.data.get("request_id") is not None:
            step = request_steps.get(str(event.data["request_id"]))
        if step is None and event.data.get("tool_call_id") is not None:
            step = tool_steps.get(str(event.data["tool_call_id"]))
        if step is None:
            ungrouped.append(event)
        else:
            step_events.setdefault(step, []).append(event)

    steps = tuple(
        _build_step(number, tuple(group))
        for number, group in sorted(step_events.items())
    )
    run_start = next((event for event in located if event.name == "run_start"), None)
    run_end = next(
        (event for event in reversed(located) if event.name == "run_end"), None
    )
    return TraceRun(located, run_start, steps, run_end, tuple(ungrouped))


def reconstruct_model_request(
    events: list[dict[str, Any]],
    step: int,
) -> dict[str, Any]:
    """Return the sanitized provider payload captured for one model turn."""

    request = next(
        (
            event
            for event in events
            if event["event"] == "model_request" and event["data"].get("step") == step
        ),
        None,
    )
    if request is None:
        raise TraceFormatError(f"trace contains no model request for step {step}")
    payload = request["data"].get("payload")
    if not isinstance(payload, dict):
        raise TraceFormatError(
            f"step {step} has no captured model payload; rerun with --trace-level debug"
        )
    return payload


def render_trace_report(
    path: Path,
    *,
    step: int | None = None,
    verbose: bool = False,
    events: bool = False,
) -> str:
    """Render a grouped report, or an explicitly requested flat event timeline."""

    run = build_trace_run(read_located_trace(path))
    selected_steps = run.steps
    if step is not None:
        selected_steps = tuple(item for item in run.steps if item.number == step)
        if not selected_steps:
            raise TraceFormatError(f"trace contains no events for step {step}")

    lines = _report_header(path, run)
    if events:
        lines.extend(_render_flat_events(run, step=step, verbose=verbose))
    else:
        max_steps = _maximum_steps(run)
        for item in selected_steps:
            lines.extend(
                _render_step(item, run, max_steps, expanded=verbose or step is not None)
            )
    return "\n".join(lines) + "\n"


def _build_step(number: int, events: tuple[LocatedTraceEvent, ...]) -> TraceStep:
    ordered = tuple(sorted(events, key=_event_order))
    requests = [event for event in ordered if event.name == "model_request"]
    responses = [
        event for event in ordered if event.name in {"assistant", "model_error"}
    ]
    request = requests[0] if requests else None
    response = _matching_response(request, responses)
    plan = next((event for event in ordered if event.name == "plan_decision"), None)
    protocol = tuple(
        event
        for event in ordered
        if event.name in {"protocol_reminder", "protocol_violation"}
    )
    tools = _pair_tools(ordered)
    return TraceStep(number, ordered, request, response, plan, protocol, tools)


def _matching_response(
    request: LocatedTraceEvent | None,
    responses: list[LocatedTraceEvent],
) -> LocatedTraceEvent | None:
    if not responses:
        return None
    if request is not None and request.data.get("request_id") is not None:
        request_id = str(request.data["request_id"])
        match = next(
            (
                response
                for response in responses
                if response.data.get("request_id") is not None
                and str(response.data["request_id"]) == request_id
            ),
            None,
        )
        if match is not None:
            return match
        return next(
            (
                response
                for response in responses
                if response.data.get("request_id") is None
            ),
            None,
        )
    return responses[0]


def _pair_tools(
    events: tuple[LocatedTraceEvent, ...],
) -> tuple[TraceToolExecution, ...]:
    calls = [event for event in events if event.name == "tool_call"]
    results = [event for event in events if event.name == "tool_result"]
    used_results: set[int] = set()
    executions: list[TraceToolExecution] = []
    for call in calls:
        call_id = call.data.get("tool_call_id")
        result_index = next(
            (
                index
                for index, result in enumerate(results)
                if index not in used_results
                and call_id is not None
                and result.data.get("tool_call_id") is not None
                and str(result.data["tool_call_id"]) == str(call_id)
            ),
            None,
        )
        if result_index is None and call_id is None:
            legacy_matches = [
                index
                for index, result in enumerate(results)
                if index not in used_results
                and result.data.get("tool") == call.data.get("tool")
            ]
            result_index = legacy_matches[0] if len(legacy_matches) == 1 else None
        elif result_index is None:
            legacy_matches = [
                index
                for index, result in enumerate(results)
                if index not in used_results
                and result.data.get("tool_call_id") is None
                and result.data.get("tool") == call.data.get("tool")
            ]
            result_index = legacy_matches[0] if len(legacy_matches) == 1 else None
        result = results[result_index] if result_index is not None else None
        if result_index is not None:
            used_results.add(result_index)
        executions.append(TraceToolExecution(call, result))
    executions.extend(
        TraceToolExecution(None, result)
        for index, result in enumerate(results)
        if index not in used_results
    )
    return tuple(executions)


def _report_header(path: Path, run: TraceRun) -> list[str]:
    start_data = run.run_start.data if run.run_start else {}
    run_id = next(
        (
            event.record.get("run_id")
            for event in run.events
            if event.record.get("run_id")
        ),
        "legacy",
    )
    model_turns = sum(event.name == "assistant" for event in run.events)
    tool_results = [event for event in run.events if event.name == "tool_result"]
    tool_errors = sum(
        _tool_result_payload(event).get("ok") is not True for event in tool_results
    )
    nonzero_commands = sum(
        isinstance(_tool_result_payload(event).get("exit_code"), int)
        and _tool_result_payload(event)["exit_code"] != 0
        for event in tool_results
    )
    reminders = sum(event.name == "protocol_reminder" for event in run.events)
    last_elapsed = max(
        (
            event.record.get("elapsed_ms", 0)
            for event in run.events
            if isinstance(event.record.get("elapsed_ms"), int)
        ),
        default=0,
    )
    observed_steps = max((step.number for step in run.steps), default=0)
    outcome = _outcome_text(run.run_end, observed_steps)
    return [
        "Yada trace report",
        "",
        f"Path: {path}",
        f"Run: {run_id}",
        f"Model: {start_data.get('model', 'unknown')}",
        f"Editing strategy: {start_data.get('editing_strategy', 'legacy')}",
        f"Trace level: {start_data.get('trace_level', 'legacy')}",
        f"Task: {_one_line(start_data.get('task', 'unknown'), 160)}",
        f"Outcome: {outcome}",
        (
            "Totals: "
            f"{model_turns} model turns, {len(tool_results)} tool results, "
            f"{tool_errors} tool errors, {nonzero_commands} non-zero commands, "
            f"{reminders} reminders, {last_elapsed} ms"
        ),
        "",
    ]


def _outcome_text(run_end: LocatedTraceEvent | None, observed_steps: int) -> str:
    if run_end is None:
        return "interrupted (no run_end event)"
    steps = _step_number(run_end.data.get("steps")) or observed_steps
    suffix = f" in {steps} {_plural(steps, 'step')}" if steps else ""
    if run_end.data.get("finished") is True:
        return f"resolved{suffix}"
    return (
        f"unfinished after {steps} {_plural(steps, 'step')}" if steps else "unfinished"
    )


def _render_step(
    step: TraceStep,
    run: TraceRun,
    max_steps: int,
    *,
    expanded: bool,
) -> list[str]:
    response = step.model_response
    start_data = run.run_start.data if run.run_start else {}
    model = (
        response.data.get("model")
        if response is not None and response.name == "assistant"
        else None
    ) or start_data.get("model", "unknown")
    label = "interrupted" if step.model_request and response is None else str(model)
    heading = f"Step {step.number}/{max_steps} — {label}"
    if response is not None and isinstance(
        response.data.get("duration_ms"), (int, float)
    ):
        heading += f"  {_duration(response.data['duration_ms'])}"
    tokens = _total_tokens(response.data.get("usage")) if response else None
    if tokens is not None:
        heading += f"  {tokens:,} tokens"
    heading += f"  {_line_range(step.first_line, step.last_line)}"
    lines = [heading]

    if step.model_request is not None or response is not None:
        response_label = (
            "error" if response and response.name == "model_error" else "response"
        )
        lines.append(
            "  Model call  "
            + _pair_reference("request", step.model_request, response_label, response)
        )
        if response is not None and response.name == "assistant":
            lines.append(
                f"    Finish reason: {response.data.get('finish_reason', '?')}"
            )
            reasoning_chars = _reasoning_length(response.data.get("message"))
            if reasoning_chars is not None:
                lines.append(f"    Reasoning: {reasoning_chars:,} chars")
        elif response is not None:
            error_type = response.data.get("error_type", "Error")
            error = _one_line(response.data.get("error", ""), 160)
            lines.append(f"    Error: {error_type}: {error}")

    if step.plan_decision is not None:
        plan = step.plan_decision
        lines.append(
            f"  Plan: {plan.data.get('action', '?')}  {_event_reference(plan)}"
        )
        if plan.data.get("rejection_error"):
            lines.append(
                "    Rejected: " + _one_line(plan.data["rejection_error"], 160)
            )

    for protocol in step.protocol_events:
        label = "Reminder" if protocol.name == "protocol_reminder" else "Violation"
        detail = protocol.data.get("text") or protocol.data.get("error") or ""
        lines.append(
            f"  Protocol {label.lower()}: {_one_line(detail, 160)}  "
            f"{_event_reference(protocol)}"
        )

    if step.tool_executions:
        lines.append("  Tools:")
        for execution in step.tool_executions:
            lines.append("    " + _tool_summary(execution))

    if expanded:
        lines.extend(_expanded_step_events(step.events))
    lines.append("")
    return lines


def _tool_summary(execution: TraceToolExecution) -> str:
    source = execution.call or execution.result
    if source is None:
        return "[missing] unknown  [call missing → result missing]"
    name = source.data.get("tool", "?")
    result_data = execution.result.data if execution.result else {}
    result = result_data.get("result") if execution.result else None
    if not isinstance(result, dict):
        status = "missing" if execution.result is None else "error"
        result = {}
    else:
        status = "ok" if result.get("ok") is True else "error"
    parts = [f"[{status}] {name}"]
    duration = result_data.get("duration_ms")
    if isinstance(duration, (int, float)):
        parts.append(_duration(duration))
    if "exit_code" in result:
        parts.append(f"exit={result.get('exit_code')}")
    parts.append(_pair_reference("call", execution.call, "result", execution.result))
    return "  ".join(parts)


def _expanded_step_events(events: tuple[LocatedTraceEvent, ...]) -> list[str]:
    lines = ["", "  Events:"]
    for event in events:
        sequence = event.record.get("sequence")
        sequence_text = f"  sequence={sequence}" if isinstance(sequence, int) else ""
        lines.append(f"    L{event.line_number} {event.name}{sequence_text}")
        if event.name == "model_request" and "payload" not in event.data:
            lines.append("      payload unavailable: rerun with --trace-level debug")
        rendered = json.dumps(
            event.data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        lines.extend(f"      {line}" for line in rendered.splitlines())
    return lines


def _render_flat_events(
    run: TraceRun,
    *,
    step: int | None,
    verbose: bool,
) -> list[str]:
    selected = run.events
    if step is not None:
        selected_lines = {
            event.line_number
            for trace_step in run.steps
            if trace_step.number == step
            for event in trace_step.events
        }
        selected = tuple(
            event for event in run.events if event.line_number in selected_lines
        )
    lines = [f"Events{f' (step {step})' if step is not None else ''}:"]
    for fallback_sequence, event in enumerate(selected, 1):
        sequence = event.record.get("sequence", fallback_sequence)
        elapsed = event.record.get("elapsed_ms", "?")
        lines.append(
            f"  L{event.line_number} [{sequence!s:>3} +{elapsed!s:>6}ms] "
            f"{_describe_event(event.record)}"
        )
        if verbose:
            rendered = json.dumps(
                event.data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
            lines.extend(f"      {line}" for line in rendered.splitlines())
    return lines


def _maximum_steps(run: TraceRun) -> int:
    start_max = run.run_start.data.get("max_steps") if run.run_start else None
    observed = max((step.number for step in run.steps), default=0)
    if isinstance(start_max, int) and start_max > 0:
        return max(start_max, observed)
    end_steps = run.run_end.data.get("steps") if run.run_end else None
    if isinstance(end_steps, int) and end_steps > 0:
        return max(end_steps, observed)
    return observed


def _event_order(event: LocatedTraceEvent) -> tuple[int, int]:
    sequence = event.record.get("sequence")
    return (
        sequence if isinstance(sequence, int) else event.line_number,
        event.line_number,
    )


def _step_number(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _event_reference(event: LocatedTraceEvent) -> str:
    return f"[L{event.line_number}]"


def _line_range(first: int, last: int) -> str:
    return f"[L{first}]" if first == last else f"[L{first}–L{last}]"


def _pair_reference(
    first_label: str,
    first: LocatedTraceEvent | None,
    second_label: str,
    second: LocatedTraceEvent | None,
) -> str:
    first_location = f"L{first.line_number}" if first else "missing"
    second_location = f"L{second.line_number}" if second else "missing"
    return f"[{first_label} {first_location} → {second_label} {second_location}]"


def _duration(milliseconds: int | float) -> str:
    if milliseconds < 1000:
        return f"{milliseconds:g}ms"
    seconds = milliseconds / 1000
    return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:g}s"


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


def _reasoning_length(message: Any) -> int | None:
    if not isinstance(message, dict) or "reasoning_content" not in message:
        return None
    reasoning = message["reasoning_content"]
    if isinstance(reasoning, dict) and isinstance(reasoning.get("chars"), int):
        return reasoning["chars"]
    if isinstance(reasoning, str):
        return len(reasoning)
    return 0 if reasoning is None else len(str(reasoning))


def _tool_result_payload(event: LocatedTraceEvent) -> dict[str, Any]:
    result = event.data.get("result")
    return result if isinstance(result, dict) else {}


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


def _describe_event(event: dict[str, Any]) -> str:
    name = event["event"]
    data = event["data"]
    step = data.get("step")
    step_text = f"step={step} " if step is not None else ""
    if name == "run_start":
        return f"run_start workspace={_one_line(data.get('workspace', '?'), 80)}"
    if name == "model_request":
        context = data.get("context") or {}
        return (
            f"{step_text}model_request messages={context.get('message_count', '?')} "
            f"chars={context.get('serialized_chars', '?')}"
        )
    if name == "assistant":
        message = data.get("message") or {}
        calls = message.get("tool_calls") or []
        names = ",".join(
            str((call.get("function") or {}).get("name") or "<missing>")
            for call in calls
        )
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")
        token_text = f" tokens={tokens}" if isinstance(tokens, int) else ""
        return (
            f"{step_text}assistant duration={data.get('duration_ms', '?')}ms "
            f"finish={data.get('finish_reason', '?')} tools={names or '-'}{token_text}"
        )
    if name == "model_error":
        return (
            f"{step_text}model_error {data.get('error_type', 'Error')}: "
            f"{_one_line(data.get('error', ''), 120)}"
        )
    if name == "plan_decision":
        tools = ",".join(str(tool) for tool in (data.get("tools") or []))
        return (
            f"{step_text}plan_decision action={data.get('action', '?')} "
            f"tools={tools or '-'}"
        )
    if name in {"tool_call", "tool_result"}:
        return (
            f"{step_text}{name} id={data.get('tool_call_id', '?')} "
            f"tool={data.get('tool', '?')}"
        )
    if name in {"protocol_reminder", "protocol_violation"}:
        return f"{step_text}{name}"
    if name in {
        "session_start",
        "session_end",
        "strategy_selected",
        "red_started",
        "red_observed",
        "test_frozen",
        "fix_started",
        "green_observed",
        "regression_verified",
        "finish_accepted",
        "verification_invalidated",
        "red_artifact_written",
    }:
        phase = data.get("phase")
        return f"{step_text}{name} phase={phase or '-'}"
    if name == "run_end":
        return (
            f"run_end finished={data.get('finished')} steps={data.get('steps')} "
            f"summary={_one_line(data.get('summary', ''), 120)}"
        )
    return f"{step_text}{name}"


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
