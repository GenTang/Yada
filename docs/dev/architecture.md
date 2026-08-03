# Yada architecture

Yada is a deliberately small DeepSeek-native coding harness: one model loop,
six tools, an append-only conversation, checked patch application, and a
verification gate. This document describes the internal boundaries contributors
must preserve.

For commands and public behavior, see the [CLI reference](../cli-reference.md).
For tests and trace inspection, see [Debugging](debugging.md).

## Design motivation

Yada is a research vehicle for testing whether a small, model-specific harness
can remain easier to audit and evaluate than a general orchestration framework.
Its working hypotheses are:

1. A small, stable tool schema reduces protocol failures.
2. SHA-bound patches reject stale edits before they mutate the workspace.
3. Structured, bounded command observations help the model recover from failures.
4. An append-only conversation preserves DeepSeek tool-call reasoning and stable
   prompt prefixes.

New abstractions should be justified by a concrete evaluation or debugging need.
Yada currently does not aim to provide a TUI, IDE integration, subagents,
long-term memory, model routing, or automatic commits.

## Package map

```text
run/cli.py
    ├── agents/default.py
    │   ├── agents/planning.py
    │   ├── agents/executor.py
    │   │   └── tools/runner.py
    │   │       ├── environments/workspace.py
    │   │       ├── environments/approval.py
    │   │       ├── environments/commands.py
    │   │       └── tools/{search,read,replace,patch,command,finish}.py
    │   ├── models/base.py ← models/deepseek.py
    │   └── traces/jsonl.py ← traces/report.py
    └── evals/cli.py
        └── evals/runner.py
            ├── evals/benchmarks/{local,swebench}.py
            └── evals/agents/{yada,command}.py
```

- `run`: parses public configuration and assembles a direct run.
- `agents`: coordinates model turns, planning policy, and tool execution.
- `models`: defines the completion boundary and DeepSeek transport.
- `environments`: owns workspace containment, command approval, and execution backends.
- `tools`: implements one handler per model-facing tool.
- `traces`: writes append-only events and builds source-located reports.
- `evals`: composes benchmark preparation/grading with interchangeable agents.
- `benchmarks`: stores reproducible recipes, not generated checkouts or results.
- `utils`: contains small mechanics shared across otherwise separate packages.

Dependencies point inward through these boundaries. A tool does not know about
the model loop, and the DeepSeek client does not know which tools execute calls.

## Agent loop

```text
stable system prompt + tool schemas
                ↓
          DeepSeek completion
                ↓
     Planner validates next action
                ↓
     Executor runs ordered tool calls
                ↓
 append assistant + tool observations
                ↓
       repeat or verified finish
```

`Agent` in `agents/default.py` owns the append-only message list, step limit,
usage accumulation, and top-level trace events. It does not implement tool
policy or workspace mutation.

The DeepSeek adapter keeps `reasoning_content` on assistant tool-call messages.
The whole assistant message is appended before tool results, so subsequent model
requests satisfy DeepSeek's thinking/tool-call contract.

## Planner and executor

The current `Planner` is deterministic; it is not another model call. It builds
the initial prompt, interprets assistant output, recovers from text-only turns,
and rejects unsafe batches such as `finish` mixed with another tool. It has no
workspace access.

The `Executor` parses tool arguments, preserves model-provided call order,
invokes `ToolRunner`, and records correlated `tool_call` and `tool_result`
events. A future planning model or remote executor can replace either side
without weakening the workspace and tool contracts.

## Tool system

Yada exposes six tools:

| Tool | Responsibility |
| --- | --- |
| `search_code` | Search repository text with ripgrep and a Python fallback. |
| `read_file` | Return bounded, numbered text plus a SHA-256 content hash. |
| `replace_text` | Apply exact unique replacements to existing UTF-8 files. |
| `apply_patch` | Validate and apply a Git-style unified diff. |
| `run_command` | Run an approved argv array and return bounded structured output. |
| `finish` | End only after verification of the latest revision. |

`ToolRunner` composes shared workspace, approval, output-limit, and verification
state. Handlers remain otherwise stateless. File paths are resolved through the
workspace boundary, which rejects absolute paths, `..` escapes, symlink escapes,
and access to `.git` or `.yada` internals.

## Patch transaction

Patch application is intentionally stricter than ordinary text replacement:

1. `read_file` returns the current file text and SHA-256.
2. The model creates a unified diff with one `diff --git` header per target.
3. `apply_patch` requires an `expected_files` entry for every target.
4. Existing files must still match the exact hash returned by `read_file`.
5. New files use the `NEW` sentinel instead of a hash.
6. Yada rejects path escapes, unsupported binary/rename/copy/mode/symlink
   operations, incomplete target declarations, and patches Git cannot check.
7. Only after validation does `git apply` mutate the workspace.

This is an optimistic transaction: a file changed after the model read it is a
conflict, not permission to apply a stale edit. Every successful patch increments
the workspace revision and invalidates earlier verification.

`replace_text` uses the same transaction rather than introducing another write
path. It validates every SHA and exact unique match in memory, applies same-file
edits in declaration order, generates a standard-library unified diff, and sends
the complete result through `apply_patch`. Zero or ambiguous matches fail closed;
no file changes until the generated multi-file patch passes validation.

## Commands and verification

`run_command` accepts argv, not a shell string. It checks the executable
allowlist, disables shell `-c`, restricts Git to read-only subcommands, applies
the configured approval policy, and then delegates execution to a small backend
interface. Direct runs and local cases use the host backend. Official
SWE-bench runs use a persistent ephemeral container made from the task's public
instance image, with the mutable workspace mounted at `/testbed`. Neither
backend adds a Python runtime dependency to Yada's core. Both sanitize
secret-looking environment variables and return stdout, stderr, exit code,
timeout, and duration through the same tool result.

The model labels a command as `inspect`, `test`, or `build`. Only a successful
`test` or `build` verifies the current workspace revision. `finish` rejects the
run when:

- no verification succeeded;
- a patch was applied after the last successful verification; or
- `git diff --check` fails.

Thus a passing test before the final edit cannot satisfy completion.

## Trace architecture

JSONL is the crash-safe source of truth. Each completed event write is visible
immediately and contains schema version, run ID, sequence, timestamp, elapsed
time, event name, and event-specific data.

The report layer keeps storage and presentation separate. It associates each
record with its physical JSONL line, groups events into model steps, and pairs
model requests/responses and tool calls/results by their correlation IDs.
`yada-trace --events` exposes the flat source timeline when the grouped view is
not enough.

Summary traces redact reasoning to a length and hash. Debug traces add sanitized
provider payloads and reasoning text. Both levels apply secret-pattern
redaction, but debug traces still contain sensitive source and model context.
See [Debugging](debugging.md) for the event vocabulary and inspection workflow.

## Evaluation architecture

The evaluation layer keeps the coding agent separate from task preparation and
grading:

```text
BenchmarkAdapter ── load/prepare ──┐
                                  ├─ EvalRunner ─ result.json
AgentAdapter ────── run/patch ─────┤
                                  └─ BenchmarkAdapter.grade
```

Its stable data boundaries are:

- `EvalTask`: public issue text and non-secret metadata;
- `PreparedTask`: isolated candidate workspace;
- `AgentRunResult`: patch, usage, steps, trace, and agent status;
- `GradeResult`: benchmark-owned verdict and test diagnostics.

The native adapter runs Yada in process. The command adapter allows comparable
external agents. Local and SWE-bench benchmark adapters own provenance, hidden
grading inputs, and the final verdict. Gold patches and hidden tests never cross
the public task boundary.

The official SWE-bench adapter deliberately uses two Docker lifetimes:

```text
public instance image
        ├── export /testbed ── host artifact workspace ── file tools
        └── ephemeral Agent container ─────────────────── run_command

collected model patch
        └── independent Harness container + evaluation patch ── verdict
```

The Agent container and grader container are never the same container. The
first contains only public repository setup; the second is created after the
agent stops and is the only environment that receives evaluation tests. A
Docker preflight happens during official task loading, before model inference.
Local cases do not use this backend unless their own manifest commands do so.

Fair comparisons use the same instance, base commit, public prompt, model
endpoint, token/cost and wall-time budgets, network policy, and official grader.
Resolve rate is the primary outcome; tokens, steps, duration, and cost are
diagnostics. See [CLI reference: `yada eval`](../cli-reference.md#yada-eval) for
command examples and [Evaluation lifecycle](../evaluation.md) for the exact
load, workspace, grading, cache, and artifact sequence.

## Core invariants

1. System prompt and tool schemas stay stable during a run.
2. Conversation messages are append-only.
3. DeepSeek reasoning is preserved across tool-call turns.
4. File mutation occurs only through a checked unified diff.
5. Existing patch targets must match their last-read SHA-256.
6. Every patch invalidates previous verification.
7. `finish` requires verification of the latest revision.
8. Trace events are append-only and self-correlating.
9. Benchmark grading happens outside the agent's tool boundary.
10. Hidden grading inputs never enter the official Agent command container.

## Security boundary

Yada's path checks, argv policy, environment sanitization, and approval gate
reduce accidental damage. Direct runs and local cases still do not isolate
arbitrary repository code: test runners, interpreters, build systems,
dependencies, and scripts can access the host permissions available to the
process.

Native official SWE-bench commands run in a disposable container and hidden
grading runs in a second one. This reduces host-environment drift and prevents
the agent from observing grading inputs, but a Docker daemon and bind-mounted
workspace remain privileged infrastructure rather than a complete security
boundary.
