# Evaluation architecture

Yada's evaluation package separates the coding agent from task preparation and
grading. The agent receives only a public problem statement and a candidate Git
workspace. The benchmark adapter owns repository provenance, hidden tests, and
the final verdict.

```text
BenchmarkAdapter ── load/prepare ──┐
                                  ├─ EvalRunner ─ result.json
AgentAdapter ────── run/patch ─────┤
                                  └─ BenchmarkAdapter.grade
```

The package has four stable data boundaries:

- `EvalTask`: public issue text and non-secret metadata.
- `PreparedTask`: an isolated candidate workspace.
- `AgentRunResult`: patch, usage, steps, trace, and agent status.
- `GradeResult`: benchmark-owned resolved, unresolved, skipped, or error verdict.

`EvalRunner` composes adapters and writes a schema-versioned result even when an
ordinary adapter error occurs.

## Local manifests

The local adapter is useful for a frozen task, private benchmark, or development
smoke test. A version 1 manifest looks like this:

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

Paths are resolved relative to the manifest. `workspace_mode=copy` verifies a
Git fixture is clean and copies the complete fixture into the run artifact
directory, including generated Git-ignored files required by its pinned
environment. Repeated agents therefore do not mutate the source fixture. The
grader runs only after the agent exits. Its return code means:

- `0`: resolved
- `1`: unresolved
- any other value: grading error

The grader command may use `{workspace}`, `{patch}`, `{run_dir}`, and `{run_id}`.

### Portable cases

`--case DIRECTORY` is shorthand for a local `DIRECTORY/case.json`. Unlike a
machine-local fixture, a portable case can declare a Git source and a locked uv
environment:

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

The Git checkout is cached under `--cache-dir` (default
`.yada/cache/evals`) and never used directly as the agent workspace. `uv sync
--locked` prepares the case environment. `install_workspace` supports modern
`editable` projects and `legacy-editable` projects that predate PEP 660.
`pythonpath` entries are resolved inside the fresh agent workspace. This makes
the Agent's own test commands and the external grader use the same locked
dependencies while exercising the modified source.

The committed pytest development case runs with:

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes
```

The outer `uv run` selects Yada's environment. The case's nested uv project
selects the task environment; `yada eval` does not otherwise assume that every
benchmark uses uv.

## Agent adapters

The native adapter constructs Yada in process and records a normal JSONL trace.
It respects step and output-token budgets. A deployment-level container or
process supervisor should enforce the hard wall-time limit.

The command adapter runs any non-interactive argv without a shell. Its template
supports `{task}`, `{task_file}`, `{workspace}`, `{output_patch}`, and
`{run_dir}`. If the external agent does not write `{output_patch}`, Yada collects
the complete Git diff, including untracked files.

Example:

```bash
yada eval \
  --benchmark local \
  --manifest /path/to/task.local.json \
  --agent command \
  --agent-name another-agent \
  --agent-command 'another-agent run --task-file {task_file} --workspace {workspace}' \
  --output results/another-agent.json
```

## SWE-bench

The SWE-bench adapter can load public instance metadata from either:

- a JSON/JSONL `--instance-file`; or
- Hugging Face when the optional `datasets` package is installed.

Gold patches, test patches, and hidden test IDs are discarded at the public task
boundary. The adapter checks out only the exact base commit, writes the official
three-field `predictions.jsonl`, and delegates grading to
`swebench.harness.run_evaluation`.

Install the official SWE-bench package and Docker separately. Then run:

```bash
yada eval \
  --benchmark swebench \
  --instance pytest-dev__pytest-10051 \
  --instance-file benchmarks/swebench_verified/pytest-10051/instance.json \
  --workspace /path/to/clean/pytest-base-repo \
  --agent yada \
  --yes \
  --swebench-python /path/to/swebench-env/bin/python \
  --output results/yada-pytest-10051.json
```

`--workspace` is an optional clean source repository. If omitted, the adapter
fetches the exact commit from GitHub. This prepares the agent workspace; it does
not replace the official Docker grading environment.

Use `--grade-mode none` to validate task preparation and prediction generation
without Docker. Such a run is `skipped`, never `resolved`.

The checked-in local grader is a fast feedback loop, not an official
SWE-bench result. Only the Docker Harness path should be used for published
resolve rates.

## Fair comparisons

Use the same instance IDs, base commits, public issue text, model endpoint,
token or cost budget, wall-time limit, network policy, and official grader for
every agent. Tool sets may differ when comparing complete agent systems, but
the result should say so. Run multiple trials when the model is stochastic.

The primary metric is resolve rate. Steps, tokens, duration, and cost are
diagnostic dimensions rather than substitutes for the benchmark verdict.
