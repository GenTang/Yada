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
  --instance-file /path/to/pytest-10051.instance.json \
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

## Fair comparisons

Use the same instance IDs, base commits, public issue text, model endpoint,
token or cost budget, wall-time limit, network policy, and official grader for
every agent. Tool sets may differ when comparing complete agent systems, but
the result should say so. Run multiple trials when the model is stochastic.

The primary metric is resolve rate. Steps, tokens, duration, and cost are
diagnostic dimensions rather than substitutes for the benchmark verdict.
