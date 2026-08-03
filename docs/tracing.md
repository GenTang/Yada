# Yada trace reference

Yada records one append-only JSON object per line. JSONL is the durable source of
truth: a completed write remains inspectable if the agent later crashes, and the
file can be streamed with ordinary tools. `yada-trace` is the terminal view over
that source data.

## Record envelope

Schema v2 records have these top-level fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer format version. Current value: `2`. |
| `run_id` | Correlates every record produced by one agent run. |
| `sequence` | One-based event order within the run. |
| `timestamp` | UTC ISO 8601 wall-clock timestamp. |
| `elapsed_ms` | Milliseconds since the `TraceWriter` was created. |
| `event` | Event type described below. |
| `data` | Event-specific object. |

Readers must ignore unknown `data` fields. Adding an optional field does not
require a schema-version increase; changing or removing an existing field does.
`TraceWriter` accepts extension event names, but the ten names below are Yada's
core event vocabulary.

## Lifecycle and correlation

```text
run_start
  step 1..N
    model_request ──→ assistant ──→ plan_decision
          │                              ├─→ protocol_reminder
          └─→ model_error                ├─→ protocol_violation
                                         └─→ tool_call ──→ tool_result
run_end
```

- `step` groups one model request, its response, planning decision, and tools.
- `request_id` joins `model_request` to `assistant` or `model_error`.
- `tool_call_id` joins each `tool_call` to its `tool_result` and to the next
  request's `role=tool` message.
- Multiple tool calls can occur in one step and retain model-provided order.
- A transport exception or process interruption can leave a trace without
  `run_end`. Readers should report this as interrupted, not successful.

The reporting layer normalizes these records into `TraceRun`, `TraceStep`,
`TraceToolExecution`, and `LocatedTraceEvent` objects. A located event pairs the
unchanged persisted record with its physical JSONL line number. The public
`read_trace()` API still returns the original event dictionaries; source location
metadata is available through `read_located_trace()` and is never written back to
JSONL.

## Core events

| Event | Cardinality and meaning | Important `data` fields |
| --- | --- | --- |
| `run_start` | Once, before the loop. | `model`, `task`, `workspace`, `max_steps`, `trace_level`, `model_config`, `provenance` |
| `model_request` | Once per attempted model turn. | `step`, `request_id`, `context`; debug traces also contain `payload` and `capture` |
| `assistant` | Once after a successful model request. | `step`, `request_id`, `duration_ms`, `message`, `usage`, response metadata, `finish_reason` |
| `model_error` | Instead of `assistant` when a request raises. | `step`, `request_id`, `duration_ms`, `error_type`, `error` |
| `plan_decision` | Once after each `assistant`. | `step`, `action`, ordered `tools`, `rejection_error` |
| `protocol_reminder` | When the assistant returns no tool call. | `step`, `text` |
| `protocol_violation` | When a tool batch is rejected before side effects. | `step`, `error`, `call_count` |
| `tool_call` | Before every attempted tool execution. | `step`, `tool_call_id`, `tool`, `arguments`; invalid calls use `raw_arguments`, rejected calls use `rejected` |
| `tool_result` | After every `tool_call`, including rejected calls. | `step`, `tool_call_id`, `tool`, `duration_ms`, `result` |
| `run_end` | Once after a graceful terminal outcome. | `finished`, `steps`, `summary`, accumulated `usage`, `final_state` |

`tool_result.data.result.ok` means that the Yada tool protocol completed. For
`run_command`, a process can still have a non-zero `exit_code`; check both fields
when looking for failures.

## Assistant message fields

An assistant response is a compound message:

- `content` is optional user-facing narration.
- `reasoning_content` is DeepSeek thinking state.
- `tool_calls` contains requested actions.

A tool-calling response commonly has non-empty `reasoning_content`, one or more
`tool_calls`, and `content=""`. That is a valid model response: the tool call is
the action for the turn. Yada appends the whole assistant message to the
conversation so DeepSeek reasoning is available on subsequent tool-call turns.

The DeepSeek adapter normalizes an omitted `role` to `"assistant"` and an omitted
`content` to `""`. Use `finish_reason` and `tool_calls` to interpret an empty
content value: on a tool-calling turn, the tool call is the model's action.

## Capture levels and sensitive data

`--trace-level summary` records event timing, response data, tool activity, and
compact request-context metrics. `--trace-level debug` additionally records the
sanitized provider request payload built by the same client method used for the
HTTP request.

Summary traces replace reasoning with its character count and SHA-256 digest.
Debug traces automatically retain reasoning because it is essential for
understanding intermediate tool-calling turns where `content` may be empty.
API-key-like fields and bearer, token, password, credential, and secret text are
redacted in both capture levels. Redaction does not make a trace public: debug
traces can contain reasoning, prompts, source code, patches, paths, and test
output.

## Inspection recipes

Default paths include a sanitized task name and the system-local time at minute
precision. A direct run resembles
`.yada/runs/fix-parser-boundary-issue__2026-08-02_20-26.jsonl`. An evaluation
stores its trace under a directory such as
`eval-results/pytest-dev__pytest-10051__2026-08-02_20-26.artifacts/`. When a
default name already exists, Yada adds `(1)`, `(2)`, and so on before the output
suffix. The result JSON and artifacts directory share the same number. Explicit
`--trace`, `--output`, and `--artifact-dir` values are never renamed.

Render a run summary or expand one step:

```bash
uv run yada-trace TRACE.jsonl
uv run yada-trace TRACE.jsonl --step 12
uv run yada-trace TRACE.jsonl --verbose
uv run yada-trace TRACE.jsonl --events
```

The default view groups events by agent step. Step headings show the complete
physical line range, model calls identify their request and response lines, and
tool executions identify their call and result lines. Protocol reminders and
violations also carry line references. Blank JSONL lines still count as physical
lines, while `sequence` remains the deterministic event-order field; the two are
not interchangeable. `--events` keeps the previous flat timeline shape and
prefixes every event with its physical line.

Once a report identifies a suspicious step or tool execution, inspect the exact
source records directly:

```bash
sed -n '31p' TRACE.jsonl
sed -n '33,34p' TRACE.jsonl
```

List event counts:

```bash
jq -r '.event' TRACE.jsonl | sort | uniq -c
```

Show the exact sanitized request payload captured for step 12:

```bash
jq 'select(.event == "model_request" and .data.step == 12) | .data.payload' \
  TRACE.jsonl
```

Show the response channels for every model turn:

```bash
jq 'select(.event == "assistant") |
  {step: .data.step,
   finish_reason: .data.finish_reason,
   content: .data.message.content,
   reasoning_content: .data.message.reasoning_content,
   tool_calls: .data.message.tool_calls}' TRACE.jsonl
```

Find rejected tools and commands with non-zero exit codes:

```bash
jq 'select(.event == "tool_result" and
  (.data.result.ok == false or ((.data.result.exit_code // 0) != 0))) |
  {step: .data.step, tool: .data.tool, result: .data.result}' TRACE.jsonl
```

## Compatibility

The reader accepts legacy records and rejects schema versions newer than it
understands. Event order and correlation identifiers are the replay contract;
timestamps are diagnostic and should not be used to reconstruct missing events.
Yada currently records enough data for deterministic inspection, not automatic
re-execution of model or shell side effects.
