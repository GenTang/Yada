# Yada architecture

Yada is a single-loop coding harness, not an orchestration framework. Its
directory boundaries make the minimal loop easier to test without turning it
into a framework of abstractions.

## Package map

```text
run/cli.py
    ├── agents/default.py
        ├── agents/planning.py
        ├── agents/executor.py
        │   └── tools/runner.py
        │       ├── environments/workspace.py
        │       ├── environments/approval.py
        │       ├── tools/search.py
        │       ├── tools/read.py
        │       ├── tools/patch.py
        │       ├── tools/command.py
        │       └── tools/finish.py
        ├── models/base.py ← models/deepseek.py
        └── traces/jsonl.py ← traces/report.py
    └── evals/cli.py
        └── evals/runner.py
            ├── evals/benchmarks/{local,swebench}.py
            ├── evals/benchmarks/local_{source,environment}.py
            └── evals/agents/{yada,command}.py
benchmarks/
    └── swebench_verified/pytest-10051/  # recipe only; no checkout or venv
```

- `agents/default.py`: coordinates state and the step limit; it owns no tool policy.
- `agents/planning.py`: side-effect-free conversation and batch protocol policy.
- `agents/executor.py`: parses, executes, and traces tool calls and their results.
- `models`: defines the completion boundary and implements DeepSeek transport.
- `environments`: owns access to the local workspace and command approval.
- `tools`: contains stateless handlers; `runner.py` composes shared tool state.
- `traces`: records correlated append-only events and renders diagnostic timelines.
- `evals`: composes benchmark preparation/grading with interchangeable agents.
- `benchmarks`: stores reproducible task recipes, canonical public inputs,
  locked task environments, and external graders.
- `run`: parses user configuration and assembles the runtime.
- `utils`: holds small mechanics shared by otherwise independent modules.

Dependencies point inward through these contracts. A tool handler does not know
about the model or agent, and the DeepSeek adapter does not know about tools.

## Invariants

1. The system prompt and tool schema stay stable for the whole run.
2. Messages are append-only; assistant `reasoning_content` is retained in memory.
3. File mutation is possible only through a checked unified diff.
4. Every existing patch target must have been read at the exact current SHA-256.
5. Every patch invalidates prior verification.
6. `finish` succeeds only if a `test` or `build` command passed at the latest revision.
7. All trace events are append-only JSONL records with a run ID and sequence.

## Planner/executor seam

The current `Planner` is a deterministic policy layer, not a second LLM call. It
builds the stable prompt prefix, recovers from text-only responses, and rejects
unsafe multi-call `finish` batches. It has no access to the workspace. The
`Executor` owns argument decoding, `ToolRunner` side effects, durations, and
tool-call correlation IDs.

This split prevents `default.py` from becoming the home of every future policy.
An experiment can replace `Planner` with an explicit plan-producing model or
state machine without changing tool safety, and can replace `Executor` for a
remote sandbox without changing conversation policy.

## Patch transaction

`read_file` returns a content hash. `apply_patch` parses every `diff --git`
header and requires an exact set of `{path, sha256}` declarations. The patch is
rejected if a file changed after it was read, if a path leaves the workspace, or
if Git cannot apply it cleanly. A new file uses the sentinel `NEW`.

This contract is intentionally stricter than a generic text-replace tool and
smaller than maintaining session-local snippet objects.

## Command observations

Commands use an argv array and capture stdout, stderr, exit code, duration, and
timeout separately. Long output keeps a larger prefix plus a suffix containing
the final error frames. Successful `inspect` commands do not satisfy the
verification gate; only `test` and `build` do.

## Trace diagnostics

JSONL is the crash-safe source of truth, but it is not the debugging interface.
Each event includes `schema_version`, `run_id`, `sequence`, UTC time, and elapsed
milliseconds. Model requests capture message count and serialized context size;
responses capture provider metadata and latency; tool calls and results share a
`tool_call_id`; `plan_decision` records the side-effect-free policy outcome.
`model_error` leaves an explicit failed request in interrupted runs.

`yada-trace PATH` validates this stream and renders a bounded timeline with run
outcome, tool failures, protocol reminders, and per-step durations. It never
echoes full prompts, arguments, reasoning, or command output into the summary.

## Security boundary

Path checks and command policy reduce accidental damage, but `python`, test
runners, build tools, and repository scripts execute arbitrary code. A real
benchmark deployment should run each task in a disposable container and run the
hidden grader in a separate container after the agent exits.

## Evaluation-driven next steps

Do not add a feature until a frozen baseline exposes a failure class. Candidate
ablations are:

1. SHA-bound patch vs ordinary unified diff.
2. Bounded structured output vs raw terminal output.
3. Append-only stable prefix vs context rebuilding.
4. Free exploration vs mandatory plan.
5. One DeepSeek model vs a cheaper exploration model followed by a stronger repair model.
