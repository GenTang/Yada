# Yada

**Yet Another DeepSeek Agent** — a small, auditable coding-agent harness built
specifically for DeepSeek V4.

[中文说明](README.zh-CN.md)

Yada is deliberately narrow: one agent, one append-only conversation, five
tools, version-checked patches, and a verification gate. The runtime has no
third-party Python dependencies. Development and tests use pytest.

> Alpha status: the offline agent loop is tested, but no comparative benchmark
> result is claimed yet.

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
after tool calls, as required by the API. JSONL traces redact the reasoning text
by default while retaining its length and hash. Use `--trace-reasoning` only if
you intentionally want to store it.

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

## Tests

The test suite includes a fully offline fake-model run through read → patch →
test → finish, plus stale hash, path escape, secret environment, and verification
gate tests.

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

pytest is a development dependency rather than a runtime dependency. Its
fixtures keep repository setup reusable, `monkeypatch` makes process-boundary
tests explicit, and plain `assert` statements produce compact failure output.

## Project layout

Yada uses a `src/` layout and keeps orchestration separate from execution:

```text
src/yada/
├── agents/        # append-only model/tool loop and prompts
├── models/        # model protocol and DeepSeek API adapter
├── environments/  # workspace boundary and command approval
├── tools/         # one module per tool plus the small dispatcher
├── traces/        # JSONL trajectory writer
├── run/           # CLI entry point
└── utils/         # bounded-output helpers
tests/
├── agents/
├── models/
└── tools/
```

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

See [docs/architecture.md](docs/architecture.md) for the detailed contracts and planned
ablations.

## Current non-goals

No TUI, IDE plugin, MCP, skills, subagents, web search, long-term memory, model
routing, automatic commits, or benchmark leaderboard. Those features should be
earned by evaluation evidence.
