# Yada architecture

Yada is a single-loop coding harness, not an orchestration framework. Its
directory boundaries make the minimal loop easier to test without turning it
into a framework of abstractions.

## Package map

```text
run/cli.py
    └── agents/default.py
        ├── models/base.py ← models/deepseek.py
        ├── tools/runner.py
        │   ├── environments/workspace.py
        │   ├── environments/approval.py
        │   ├── tools/search.py
        │   ├── tools/read.py
        │   ├── tools/patch.py
        │   ├── tools/command.py
        │   └── tools/finish.py
        └── traces/jsonl.py
```

- `agents`: owns conversation state, the step limit, and tool-call protocol.
- `models`: defines the completion boundary and implements DeepSeek transport.
- `environments`: owns access to the local workspace and command approval.
- `tools`: contains stateless handlers; `runner.py` composes shared tool state.
- `traces`: records replay-oriented, append-only events.
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
7. All trace events are append-only JSONL records.

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

