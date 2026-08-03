# CLI reference

Yada installs two entry points:

- `yada`: run the coding agent or an evaluation;
- `yada-trace`: inspect a JSONL execution trace.

Examples below use `uv run`. After editable pip installation, omit `uv run`.
Run `uv run yada --help`, `uv run yada eval --help`, or
`uv run yada-trace --help` for the parser-generated reference.

## `yada`

```text
yada TASK [OPTIONS]
yada --task-file FILE [OPTIONS]
```

Provide exactly one task source. The workspace defaults to the current directory.

```bash
uv run yada "Fix the parser boundary case and run its tests" \
  --workspace /path/to/repository

uv run yada --task-file issue.md --workspace /path/to/repository
```

### Options

| Option | Meaning | Default |
| --- | --- | --- |
| `TASK` | Natural-language coding task. | — |
| `--task-file PATH` | Read the task from a UTF-8 file. | — |
| `--workspace PATH` | Target Git workspace. | Current directory |
| `--model NAME` | DeepSeek model name. | `DEEPSEEK_MODEL` or `deepseek-v4-pro` |
| `--base-url URL` | DeepSeek-compatible API base URL. | `DEEPSEEK_BASE_URL` or `https://api.deepseek.com` |
| `--reasoning-effort high\|max` | Thinking effort. | `max` |
| `--thinking` / `--no-thinking` | Enable or disable thinking. | Enabled |
| `--max-steps N` | Maximum model turns. | `30` |
| `--max-output-tokens N` | Maximum tokens requested per completion. | `16384` |
| `--api-timeout SECONDS` | Timeout for one model request. | `300` |
| `--command-timeout SECONDS` | Default repository-command timeout. | `120` |
| `--command-policy ask\|allow\|deny` | Repository-command approval policy. | `ask` |
| `--yes` | Alias for command policy `allow`. | Off |
| `--trace PATH` | Exact JSONL trace path. | `.yada/runs/<task>__<time>.jsonl` |
| `--trace-level summary\|debug` | Trace capture detail. | `summary` |
| `--version` | Print the Yada version. | — |

Exit status is `0` after verified completion, `2` when the step limit or another
unfinished outcome is reached, `3` for a DeepSeek API error, and `130` after
keyboard interruption. Argument errors also use the standard argparse status `2`.

## `yada eval`

`yada eval` runs an agent behind a benchmark-neutral adapter and persists a
schema-versioned result even for ordinary adapter failures.

### Checked-in development case

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes
```

The first run fetches the pinned pytest commit and builds the case's locked
Python environment. Later runs reuse caches but create a fresh agent workspace.
This local grader is a development feedback loop, not an official SWE-bench
Docker score.

### Common options

| Option | Meaning | Default |
| --- | --- | --- |
| `--benchmark local\|swebench` | Benchmark adapter. | Inferred from `--case`, otherwise required |
| `--case PATH` | Portable local case directory or `case.json`. | — |
| `--instance ID` | Benchmark instance ID. | Manifest ID for local cases |
| `--agent yada\|command` | Agent adapter. | `yada` |
| `--output PATH` | Result JSON path. | `eval-results/<task>__<time>.json` |
| `--artifact-dir PATH` | Workspace, logs, patch, trace, and grader artifacts. | Sibling `<result>.artifacts` |
| `--run-id ID` | Stable correlation ID. | Generated |
| `--max-steps N` | Model-turn budget. | `30` |
| `--wall-time SECONDS` | Comparable wall-time budget. | `1800` |
| `--max-output-tokens N` | Per-completion token limit. | `16384` |

The native agent also accepts the model, thinking, timeout, command-policy, and
trace-level options documented for `yada`. A deployment-level supervisor should
enforce a hard wall-time limit for an in-process native agent.

Evaluation exits with `0` for `resolved`, `1` for `unresolved`, and `2` for
errors or non-verdict outcomes such as skipped grading.

### Local manifest

Use `--benchmark local --manifest FILE` for a private or machine-local task.
A minimal version 1 manifest is:

```json
{
  "schema_version": 1,
  "instance_id": "example__repo-1",
  "task_file": "task.md",
  "workspace": "candidate",
  "workspace_mode": "copy",
  "base_commit": "0123456789abcdef",
  "grader": {
    "argv": ["python", "grader.py", "{workspace}"],
    "timeout_seconds": 1800
  }
}
```

Paths are relative to the manifest. `workspace_mode=copy` preserves the source
fixture and gives each run a fresh copy. An in-place workspace is also supported
for explicitly local experiments. The grader runs after the agent exits:

- exit `0`: resolved;
- exit `1`: unresolved;
- any other exit: grading error.

Grader argv entries may use `{workspace}`, `{patch}`, `{run_dir}`, and `{run_id}`.

A portable case can replace `workspace` with a pinned Git source and define a
locked uv environment:

```json
{
  "schema_version": 1,
  "instance_id": "owner__repo-1",
  "instance_file": "instance.json",
  "workspace": {
    "type": "git",
    "url": "https://github.com/owner/repo.git",
    "cache_key": "verified/owner__repo-1"
  },
  "base_commit": "0123456789abcdef",
  "environment": {
    "type": "uv",
    "project": ".",
    "install_workspace": "editable",
    "pythonpath": ["src"]
  },
  "grader": {
    "argv": [".venv/bin/python", "grader.py", "{workspace}"]
  }
}
```

Portable Git checkouts are cached under `--cache-dir` (default
`.yada/cache/evals`) but never used directly as the mutable agent workspace.
`install_workspace` accepts `editable` and `legacy-editable`.

### External command agent

The command adapter runs a non-interactive argv without a shell:

```bash
uv run yada eval \
  --benchmark local \
  --manifest /path/to/task.local.json \
  --agent command \
  --agent-name another-agent \
  --agent-command \
    'another-agent run --task-file {task_file} --workspace {workspace}' \
  --output results/another-agent.json
```

The template supports `{task}`, `{task_file}`, `{workspace}`, `{output_patch}`,
and `{run_dir}`. If the command does not write `{output_patch}`, Yada collects
the complete Git diff, including untracked files.

### SWE-bench

The SWE-bench adapter loads public instance metadata from `--instance-file`, or
from Hugging Face when the optional `datasets` package is installed. It discards
gold patches, test patches, and hidden test IDs at the public task boundary.

Install the official SWE-bench package and Docker separately, then run:

```bash
uv run yada eval \
  --benchmark swebench \
  --instance pytest-dev__pytest-10051 \
  --instance-file benchmarks/swebench_verified/pytest-10051/instance.json \
  --workspace /path/to/clean/pytest-base-repo \
  --agent yada \
  --yes \
  --swebench-python /path/to/swebench-env/bin/python \
  --output results/yada-pytest-10051.json
```

`--workspace` is optional; without it, Yada fetches the exact base commit. It
prepares the candidate workspace but does not replace official Docker grading.
Use `--grade-mode none` to test preparation and prediction generation without
Docker; the result is `skipped`, never `resolved`.

Other SWE-bench options are `--dataset-name`, `--split`, `--cache-level`,
`--clean`, `--namespace`, and `--grade-timeout`. Use the same task, base commit,
model budget, network policy, and official grader when comparing agents.

## `yada-trace`

```text
yada-trace TRACE.jsonl [--step N | --verbose | --events]
```

| Option | Meaning |
| --- | --- |
| `--step N` | Expand one complete request → response → tools step. |
| `--verbose` | Expand payloads inside every grouped step. |
| `--events` | Show the flat event timeline with physical JSONL line numbers. |

The default view groups records by agent step and shows physical JSONL line
references for the model request/response and each tool call/result pair:

```bash
uv run yada-trace TRACE.jsonl
uv run yada-trace TRACE.jsonl --step 8
uv run yada-trace TRACE.jsonl --events
```

`yada-trace` returns `0` after rendering and `2` for an unreadable or invalid
trace. See the [debugging guide](dev/debugging.md) for event semantics, trace
levels, `jq` recipes, and reproduction workflows.

## Output naming

Default direct-run and evaluation names contain a sanitized task name and the
system-local time at minute precision. If a name exists, Yada appends `(1)`,
`(2)`, and so on. An evaluation result JSON and its `.artifacts` directory always
share the same collision number. Explicit `--trace`, `--output`, and
`--artifact-dir` paths are never renamed.
