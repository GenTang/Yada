# Debugging Yada

This guide is for contributors diagnosing agent behavior, tests, traces, and
reproducible benchmark failures. Start with [CONTRIBUTING.md](../../CONTRIBUTING.md)
for environment setup and the [architecture](architecture.md) for component
boundaries.

## Fast diagnosis workflow

1. Reproduce the failure with the smallest deterministic test or checked-in case.
2. Capture a `debug` trace when model context matters.
3. Use `yada-trace` to find the failing agent step.
4. Follow the displayed `L<number>` references into the source JSONL.
5. Add an offline regression test before changing behavior.
6. Run the full local CI suite.

## Run tests

Install development dependencies once:

```bash
uv sync --locked --dev
```

Run a focused file or test while iterating:

```bash
uv run --frozen pytest tests/traces/test_trace_report.py -v
uv run --frozen pytest \
  tests/traces/test_trace_report.py::test_step_report_groups_correlated_events -v
```

Run all local checks before pushing:

```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest tests/ -v
```

The suite includes a fully offline fake-model path through read → patch → test →
finish. Prefer fake clients and temporary Git repositories for agent-loop tests;
unit tests must not require a DeepSeek key or network access.

## Capture a useful trace

Direct run:

```bash
uv run yada "Fix the parser boundary issue and run the tests" \
  --workspace /path/to/repository \
  --trace-level debug
```

Checked-in evaluation case:

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes \
  --trace-level debug
```

Trace levels are fixed policies:

| Level | Reasoning | Model request payload |
| --- | --- | --- |
| `summary` | Length and SHA-256 only | Context size metrics |
| `debug` | Sanitized text | Complete sanitized provider payload |

Use `debug` when investigating prompt construction, DeepSeek thinking, tool-call
selection, or context growth. API-key-like text is redacted at both levels, but
debug traces can contain source code, patches, prompts, reasoning, local paths,
and test output. Do not attach one publicly without reviewing it.

## Inspect by agent step

Start with the grouped report:

```bash
uv run yada-trace TRACE.jsonl
```

It groups one model request, response, planning decision, and ordered tool batch
into each step. Physical source locations connect the summary to JSONL evidence:

```text
Step 3/30 — DeepSeek  1.8s  1,246 tokens  [L30–L36]
  Model call  [request L30 → response L31]
    Finish reason: tool_calls
    Reasoning: 381 chars
  Tools:
    [ok] search_code  42ms  [call L33 → result L34]
    [error] run_command  1.4s  exit=1  [call L35 → result L36]
```

Expand the suspicious step or every grouped event:

```bash
uv run yada-trace TRACE.jsonl --step 3
uv run yada-trace TRACE.jsonl --verbose
```

Show the flat, line-prefixed event timeline when correlation itself is under
investigation:

```bash
uv run yada-trace TRACE.jsonl --events
```

For longer runs, generate a portable semantic view and open it directly in a
browser:

```bash
uv run yada-trace TRACE.jsonl --html trace.html
```

The single HTML file works offline and groups requests, reasoning, responses,
plans, tool calls/results, failures, and the final diff by step. Search and
filters run locally in the browser. Large prompts, patches, and command output
are collapsed by default. The viewer preserves redaction from the JSONL and
cannot recover omitted or redacted fields; newer traces also show whether each
assistant message field was present or explicitly normalized from omission.

Then inspect exact records using the line references:

```bash
sed -n '31p' TRACE.jsonl
sed -n '33,34p' TRACE.jsonl
```

Line references are physical, one-based JSONL lines. Blank lines count. The
persisted `sequence` field is deterministic event order and is not assumed to be
the physical line number.

## Event lifecycle

Yada stores one append-only JSON object per event:

```text
run_start
  step 1..N
    model_request ──→ assistant ──→ plan_decision
          │                              ├─→ protocol_reminder
          └─→ model_error                ├─→ protocol_violation
                                         └─→ tool_call ──→ tool_result
run_end
```

Every schema v2 record contains `schema_version`, `run_id`, `sequence`, UTC
`timestamp`, `elapsed_ms`, `event`, and an event-specific `data` object.

| Event | Meaning | Main correlation |
| --- | --- | --- |
| `run_start` | Task, workspace, model config, trace level, and provenance. | `run_id` |
| `model_request` | Attempted model turn; debug adds `payload`. | `step`, `request_id` |
| `assistant` | Model message, usage, metadata, finish reason, and latency. | `step`, `request_id` |
| `model_error` | Model request exception instead of an assistant response. | `step`, `request_id` |
| `plan_decision` | Deterministic interpretation of the assistant message. | `step` |
| `protocol_reminder` | Text-only turn caused Yada to remind the model. | `step` |
| `protocol_violation` | Tool batch rejected before side effects. | `step` |
| `tool_call` | Parsed or rejected tool request. | `step`, `tool_call_id` |
| `tool_result` | Structured outcome for one tool call. | `step`, `tool_call_id` |
| `run_end` | Outcome, steps, usage, summary, and final Git state. | `run_id` |

Several tool calls can belong to one step. A crash or transport exception may
leave no `run_end`; the report marks that trace interrupted rather than treating
it as success. It also shows missing responses or unmatched tool results.

## Assistant response channels

A DeepSeek assistant message can contain three different channels:

- `reasoning_content`: thinking state;
- `tool_calls`: requested actions;
- `content`: optional narration or final user-facing text.

During tool use, non-empty reasoning plus `content=""` is normal; the tool call is
the action. Yada appends the complete assistant message before tool results so
reasoning remains available on later tool-call turns.

## Query raw JSONL

Count event types:

```bash
jq -r '.event' TRACE.jsonl | sort | uniq -c
```

Read the exact sanitized request captured for step 12:

```bash
jq 'select(.event == "model_request" and .data.step == 12) | .data.payload' \
  TRACE.jsonl
```

Inspect response channels:

```bash
jq 'select(.event == "assistant") |
  {step: .data.step,
   finish_reason: .data.finish_reason,
   content: .data.message.content,
   reasoning_content: .data.message.reasoning_content,
   tool_calls: .data.message.tool_calls}' TRACE.jsonl
```

Find rejected tools and non-zero command exits:

```bash
jq 'select(.event == "tool_result" and
  (.data.result.ok == false or ((.data.result.exit_code // 0) != 0))) |
  {step: .data.step, tool: .data.tool, result: .data.result}' TRACE.jsonl
```

For `run_command`, `result.ok` means the Yada tool protocol completed; the child
process can still have a non-zero `exit_code`. Check both.

## Reproduce an evaluation failure

The checked-in pytest case is the shared development baseline:

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes \
  --trace-level debug
```

Keep the case, model, endpoint, base commit, budgets, command policy, and network
policy fixed when comparing a change. The result JSON records status and metadata;
its sibling `.artifacts` directory contains the workspace, patch, grader output,
and `yada-trace.jsonl`.

The local case verdict is useful for development but is not an official
SWE-bench score. Use the official Docker grader for published results; see the
[`yada eval` reference](../cli-reference.md#yada-eval).

## Common failure signals

- **`DEEPSEEK_API_KEY is not set`**: export the key in the shell launching Yada.
- **No request payload in a trace**: rerun with `--trace-level debug`.
- **`finish` rejected**: run a successful `test` or `build` after the latest patch.
- **Tool reports `ok` but tests failed**: inspect the command `exit_code`.
- **No `run_end`**: the process was interrupted or raised outside a graceful path.
- **Step limit reached**: inspect repeated reminders, failed tools, context growth,
  and the final unfinished summary before increasing the limit.
- **Patch rejected as stale**: the target changed after its last `read_file`; read
  it again and build a patch against the new hash.

## Report a reproducible issue

Include:

- the exact command with secrets removed;
- Yada commit/version, OS, Python, and uv versions;
- task or benchmark case ID and base commit;
- expected and actual behavior;
- the smallest relevant `yada-trace --step N` output;
- referenced JSONL lines after manual secret/source review;
- whether the failure reproduces with the checked-in baseline or an offline test.

Do not publish API keys, authorization headers, private source, or unreviewed
reasoning. The trace reader accepts legacy records, but a newer unsupported
schema is rejected explicitly rather than guessed.
