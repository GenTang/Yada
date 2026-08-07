# Red-Green verification

Yada implements Issue #23 as a deterministic Python Host state machine. It is two
sequential model sessions, not a Router Agent or a general multi-agent framework.

## Strategy selection

`select_strategy` is present in every model request and accepts exactly
`red_green` or `direct_execute`. Repository search, reads, and read-only Git
inspection are available before selection. Editing is not. A successful selection
is recorded once and is irreversible.

`direct_execute` continues in the same model session and uses the existing
latest-revision verification gate.

## Red phase

Selecting `red_green` requires a clean Git workspace with a valid HEAD. Yada
creates a detached temporary worktree at that revision and moves the current
session's tools into it. The canonical workspace remains unchanged until Red is
valid.

The mutation boundary classifies patch targets deterministically. Red edits may
touch only common test paths such as `tests/`, `test_*.py`, `*_test.*`, and
`*.spec.*`. A mixed test/production patch is rejected transactionally.

The model submits the exact target identity and argv through `submit_red_test`.
Yada rejects passing, skipped, uncollected, syntax, import, dependency, timeout,
and infrastructure outcomes. A valid behavioral failure freezes:

- baseline Git revision;
- cumulative test patch and SHA-256;
- test file content manifest;
- target identity;
- normalized command fingerprint;
- exit code and bounded output.

Invalid observations leave the session in Red so it may repair the test within
the remaining global step budget. Reaching `max_steps` without a valid Red ends
the run unfinished and discards the temporary worktree. It never falls back to
`direct_execute`.

## Freeze and Fix

After valid Red, Yada checks that the canonical workspace still matches the clean
baseline and applies the frozen test patch there. The patch is also written next
to the JSONL trace as `<trace-name>.red-test.patch`.

The Red message list is then abandoned. A fresh Fix message list contains only:

- the original task;
- baseline revision;
- frozen patch, manifest identity, and SHA;
- target and exact command;
- bounded Red output.

Frozen test files are denied at the patch mutation boundary. The Fix session may
edit production files. A successful `run_command` with
`verification_role=target` counts as Green only when its fingerprint exactly
matches Red. A different successful test/build command with
`verification_role=regression` supplies regression evidence. Any later production
patch clears both observations.

`finish_task` requires the frozen manifest to remain identical, a production
patch, Green and regression evidence for the latest revision, and a clean
`git diff --check` result.

## Trace

Both sessions share one `TraceWriter`, run ID, JSONL file, and global step
sequence. Events include `session_id`, `session_step`, and `phase`. A successful
run records:

```text
strategy_selected
red_started
red_observed
test_frozen
fix_started
green_observed
regression_verified
finish_accepted
```

`yada-trace TRACE.jsonl --html` displays the complete workflow and provides Red
and Fix phase filters. Debug traces may retain sanitized Red reasoning for audit,
but that trace data is never used to construct the Fix model request.
