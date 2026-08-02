# Yada

**Yet Another DeepSeek Agent** — a small, auditable coding-agent harness built
specifically for DeepSeek V4.

[中文说明](README.zh-CN.md)

Yada is deliberately narrow: one agent loop, separate planning and execution
boundaries, one append-only conversation, five tools, version-checked patches,
and a verification gate. The runtime has no third-party Python dependencies.
Development checks use Ruff and pytest.

> Alpha status: the offline agent loop is tested, but no comparative benchmark
> result is claimed yet.

## Generic evaluation

Yada includes a benchmark-neutral evaluation layer. `EvalRunner` composes any
`BenchmarkAdapter` with any `AgentAdapter`; the initial adapters cover local
JSON manifests, SWE-bench, native Yada, and arbitrary external commands.

The repository includes one portable SWE-bench Verified development case. Its
first run fetches the exact pytest commit and creates a locked Python 3.9 task
environment; later runs reuse those caches while keeping each agent workspace
fresh:

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes
```

This produces a real local verdict from one FAIL_TO_PASS and 15 PASS_TO_PASS
tests, but it is not an official Docker score. The checkout lives under
`.yada/cache/evals/`; the task recipe and its own `uv.lock` are committed.

For SWE-bench, Yada produces the patch and official `predictions.jsonl` while
delegating the verdict to the Docker-based `swebench.harness.run_evaluation`.
See [docs/evaluation.md](docs/evaluation.md) for manifests, external-agent
templates, Docker prerequisites, and fair-comparison constraints.

## Why this exists

General-purpose harnesses can run DeepSeek, but they are not necessarily shaped
around DeepSeek's tool-use and context behavior. Yada is a compact research
vehicle for testing model-native harness ideas with reproducible trajectories
and ablations.

The current hypotheses are:

1. A tiny, stable tool schema reduces tool-call failures.
2. SHA-bound unified diffs prevent stale and ambiguous edits.
3. Structured, bounded command observations improve recovery after test failures.
4. An append-only conversation preserves DeepSeek prefix-cache opportunities.

## Quick start

Requirements: Python 3.11+, Git, and a DeepSeek API key.

The recommended workflow uses [uv](https://docs.astral.sh/uv/):

```bash
cd Yada
uv sync --locked --dev
export DEEPSEEK_API_KEY="sk-..."

uv run yada "Fix the failing parser edge case and run the relevant tests" \
  --workspace /path/to/repository
```

The package also works with standard library tooling and pip:

```bash
cd Yada
python3 -m venv .venv
.venv/bin/python -m pip install -e .
export DEEPSEEK_API_KEY="sk-..."

yada "Fix the failing parser edge case and run the relevant tests" \
  --workspace /path/to/repository
```

Yada asks before every repository command by default. For autonomous execution
inside a disposable sandbox:

```bash
yada "Fix the issue described in issue.md" \
  --workspace /workspace \
  --yes
```

You can also pass a task file:

```bash
yada --task-file issue.md --workspace .
```

The default model is `deepseek-v4-pro`, thinking is enabled, and reasoning
effort is `max`. These can be changed with `--model`, `--no-thinking`, and
`--reasoning-effort`.

## Docker

The container limits filesystem exposure to the mounted repository. It is not a
network sandbox.

```bash
docker build -t yada .
docker run --rm -it \
  -e DEEPSEEK_API_KEY \
  -v "/path/to/repository:/workspace" \
  yada "Fix the failing test" --workspace /workspace --yes
```

## The loop

```text
stable prompt + tool schema
          ↓
DeepSeek tool call
          ↓
validate → approve → execute
          ↓
bounded structured observation
          ↓
append and repeat
          ↓
finish only after post-patch verification
```

Tools:

- `search_code`: ripgrep-backed repository search, with a Python fallback.
- `read_file`: bounded numbered reads plus SHA-256.
- `apply_patch`: Git-style unified diffs checked against every file hash.
- `run_command`: argv-only command execution with an allowlist and approval gate.
- `finish`: rejected until a test or build succeeds after the latest patch.

DeepSeek thinking-mode `reasoning_content` is retained in memory and passed back
after tool calls, as required by the API. The default `--trace-level summary`
records compact context metrics. `--trace-level debug` additionally records the
complete sanitized provider payload and reasoning text for every model turn.
Summary traces replace reasoning with its length and hash. Both levels redact
common API keys, authorization values, tokens, passwords, and secrets. Debug
traces contain sensitive model context and must be handled accordingly.

Capture a replayable debug trace during an evaluation:

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes \
  --trace-level debug
```

Inspect a completed or interrupted run without manually scanning JSONL:

```bash
uv run yada-trace \
  .yada/runs/fix-parser-edge-case__2026-08-02_12-26-26.123456Z.jsonl
uv run yada-trace \
  eval-results/pytest-dev__pytest-10051__2026-08-02_12-26-26.123456Z.artifacts/yada-trace.jsonl \
  --step 8
uv run yada-trace eval-results/<task>__<UTC-time>.artifacts/yada-trace.jsonl \
  --verbose
```

The report correlates model requests, tool-call IDs, errors, reminders, and the
final verification state into a compact timeline. `--step` and `--verbose`
expand sanitized model messages, tool arguments, patches, stdout, and stderr.
The source JSONL remains the durable, streaming-friendly record. Debug traces can
contain source code and test output even after secret redaction, so handle them as
sensitive artifacts. See [docs/tracing.md](docs/tracing.md) for the event
reference, field-presence semantics, lifecycle, and `jq` recipes.

## Safety model

Yada provides guardrails, not a complete OS sandbox:

- File tools reject workspace escapes, symlink escapes, `.git`, and `.yada`.
- Patches reject binary, rename, copy, mode, and symlink changes.
- Commands use argv arrays rather than a shell string.
- Shell `-c` and mutating Git subcommands are rejected.
- Secret-looking environment variables, including the DeepSeek key, are removed
  from child command environments.
- Command execution asks for confirmation unless `--yes` is used.

Repository tests are arbitrary code. Run unfamiliar repositories in a disposable
VM or a stronger sandbox. The included Dockerfile reduces filesystem exposure,
but repository code can still access the container network.

## Development checks

The test suite includes a fully offline fake-model run through read → patch →
test → finish, plus stale hash, path escape, secret environment, and verification
gate tests. Ruff provides the repository's lint and formatting gates.

```bash
uv sync --locked --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest tests/ -v
```

CI runs the same checks on Python 3.11 and 3.12. Without uv, install the runtime
project with `python3 -m pip install -e .`, install `pytest` and `ruff` separately,
then run the equivalent commands. These tools remain development dependencies and
do not increase Yada's runtime dependency footprint.

## Project layout

Yada uses a `src/` layout and keeps orchestration separate from execution:

```text
src/yada/
├── agents/        # thin loop, side-effect-free planner, and tool executor
├── models/        # model protocol and DeepSeek API adapter
├── environments/  # workspace boundary and command approval
├── tools/         # one module per tool plus the small dispatcher
├── traces/        # JSONL writer plus a human-readable diagnostic report
├── evals/         # generic runner plus benchmark and agent adapters
├── run/           # CLI entry point
└── utils/         # bounded-output helpers
benchmarks/        # portable recipes; generated checkouts stay in .yada/cache
tests/
├── agents/
├── evals/
├── models/
├── tools/
├── traces/
└── utils/
```

`Planner` owns conversation policy and validates the next action without I/O.
`Executor` owns argument parsing, workspace side effects, and correlated tool
events. `Agent` only coordinates the two. This is a deliberately small seam—not
a second model call—but it prevents the main loop from accumulating every future
planning and execution policy.

The package boundaries follow the useful parts of mini-SWE-agent's structure,
while Yada retains its own multi-tool protocol, SHA-bound patches, command
policy, and verification gate.

## Design lineage

Yada learns from the simplicity of
[mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent), the reproducible
trajectory discipline of [SWE-agent](https://github.com/SWE-agent/SWE-agent),
and DeepSeek's official [thinking-mode](https://api-docs.deepseek.com/guides/thinking_mode)
and [tool-call](https://api-docs.deepseek.com/guides/tool_calls) contracts. The
implementation is original and intentionally smaller than those systems.

See [docs/architecture.md](docs/architecture.md) for the detailed contracts and
planned ablations, and [docs/tracing.md](docs/tracing.md) for the trace schema.

## Current non-goals

No TUI, IDE plugin, MCP, skills, subagents, web search, long-term memory, model
routing, automatic commits, or benchmark leaderboard. Those features should be
earned by evaluation evidence.
