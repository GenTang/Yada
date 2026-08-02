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

## Core events

| Event | Cardinality and meaning | Important `data` fields |
| --- | --- | --- |
| `run_start` | Once, before the loop. | `model`, `task`, `workspace`, `max_steps`, `trace_level`, `model_config`, `provenance` |
| `model_request` | Once per attempted model turn. | `step`, `request_id`, `context`; debug traces also contain `payload` and `capture` |
| `assistant` | Once after a successful model request. | `step`, `request_id`, `duration_ms`, `message`, `message_field_presence`, `usage`, response metadata, `finish_reason` |
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
`content` to `""`. The `assistant.data.message_field_presence` object preserves
what the provider actually sent before normalization:

```json
{
  "message": {"role": "assistant", "content": "", "tool_calls": []},
  "message_field_presence": {
    "role_present": true,
    "content_present": false,
    "reasoning_content_present": false,
    "tool_calls_present": true
  }
}
```

Interpret `content` with its presence flag:

| Normalized value | `content_present` | Meaning |
| --- | --- | --- |
| `""` | `true` | The provider explicitly returned an empty string. |
| `""` | `false` | The provider omitted the field; Yada supplied its default. |
| non-empty string | `true` | The provider returned narration. |

The metadata is present for adapters that expose upstream field presence. Older
traces and custom completion clients can omit it.

## Capture levels and sensitive data

`--trace-level summary` records event timing, response data, tool activity, and
compact request-context metrics. `--trace-level debug` additionally records the
sanitized provider request payload built by the same client method used for the
HTTP request.

Reasoning is replaced by its character count and SHA-256 digest unless
`--trace-reasoning` is explicit. API-key-like fields and bearer, token, password,
credential, and secret text are redacted in both capture levels. Redaction does
not make a trace public: prompts, source code, patches, paths, and test output can
remain sensitive.

## Inspection recipes

Render a run summary or expand one step:

```bash
uv run yada-trace TRACE.jsonl
uv run yada-trace TRACE.jsonl --step 12
uv run yada-trace TRACE.jsonl --verbose
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

Show the response channels and upstream field presence for every model turn:

```bash
jq 'select(.event == "assistant") |
  {step: .data.step,
   finish_reason: .data.finish_reason,
   message_field_presence: .data.message_field_presence,
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
