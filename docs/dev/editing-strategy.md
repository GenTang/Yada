# Editing Strategies and Recovery Policy

## Status

This document is the implementation contract for
[Issue #10: replace-first routing with apply_patch fallback](https://github.com/GenTang/Yada/issues/10).
It describes the behavior that must remain stable across prompts, tools, traces,
tests, and evaluations. It intentionally does not prescribe Python classes or
duplicate the Agent loop as near-executable pseudocode.

The central rule is:

> The model chooses how to express an edit. Yada exposes a stable strategy,
> executes edits through existing fail-closed tools, and ensures that a failed
> edit is observed by the model before another edit can run.

## 1. Scope

### 1.1 Goals

Issue #10 adds:

1. Explicit run-level patch-only and replace-first strategies.
2. A stable tool interface and strategy prompt for the complete run.
3. A documented rule for choosing replace_text or apply_patch.
4. A later-turn recovery policy for structured edit failures.
5. A small execution barrier that prevents same-turn fallback.
6. Trace fields and evaluation metrics for comparing both strategies.
7. Deterministic tests using mocked model and tool interactions.

The existing SHA checks, transactional application, revision accounting, and
verification gate remain unchanged.

### 1.2 Non-goals

Issue #10 does not add:

- fuzzy or whitespace-normalized matching;
- automatic selection between ambiguous matches;
- AST-based routing;
- an external edit model;
- a public orchestration tool;
- framework-generated patches;
- automatic conversion of a failed replacement into a patch;
- a guarantee that the model will eventually produce a valid edit.

## 2. Terms

### Agent turn

One assistant response and the tool calls proposed by that response.

### Editing operation

A replace_text or apply_patch call. Earlier discussions use mutation for the
same concept: an operation that changes workspace files.

### Fallback

An apply_patch call deliberately proposed by the model in a later Agent turn
after it has observed a failed replace_text result and any required fresh read.

Fallback is not an internal call from replace_text to apply_patch. The
replace_text implementation may continue to reuse the validated patch boundary
to commit an already validated replacement transaction; that is an
implementation detail, not strategy fallback.

### Recovery

The model observes a structured edit error, gathers required current context,
and proposes a corrected edit in a later turn.

## 3. Required invariants

The implementation must preserve these invariants:

1. **Stable strategy:** editing_strategy is selected at run start and cannot
   change during the run.
2. **Stable interface:** the prompt and exposed tool schemas do not change
   during the run.
3. **Model-owned routing:** Yada does not use a second model, AST router, or
   hidden conversion to choose an editing representation.
4. **One edit per turn:** at most one editing operation may execute from one
   assistant response.
5. **Observed failure:** a failed edit result is included in a later model
   request before another edit may execute.
6. **Read visibility:** a read and an edit generated in the same assistant
   response cannot use that read as recovery evidence. The model had not seen
   the read result when it generated the edit.
7. **No hidden fallback:** a failed replace_text never causes Yada to generate
   or execute apply_patch.
8. **Fail closed:** a failed edit does not change files, revision, touched-file
   accounting, or verification state.
9. **Verification after success:** every successful edit invalidates previous
   verification.
10. **Bounded run:** max_steps is the final termination boundary. A small
    per-revision edit-failure limit may end an unproductive recovery earlier.

These are control-flow and safety properties. They do not guarantee that the
model follows the preferred routing policy or completes the task.

## 4. Run-level strategies

### 4.1 Initialization

The CLI and evaluation adapter accept:

~~~text
--editing-strategy patch-only
--editing-strategy replace-first
~~~

patch-only remains the default until benchmark evidence supports changing it.

The strategy-specific tool collection is built once at run initialization:

| Strategy | Exposed editing tools |
| --- | --- |
| patch-only | apply_patch |
| replace-first | replace_text and apply_patch |

Both strategies also expose search_code, read_file, run_command, and finish.
Recovery state must not add, remove, reorder, or redefine tools later in the
run.

The run-start trace records at least:

~~~json
{
  "editing_strategy": "replace-first",
  "tool_names": [
    "search_code",
    "read_file",
    "replace_text",
    "apply_patch",
    "run_command",
    "finish"
  ],
  "max_edit_failures_per_revision": 4
}
~~~

Prompt and schema fingerprints are not required for the first implementation.
The frozen strategy and tool names, combined with the existing model request
trace, are sufficient to audit the comparison.

### 4.2 Routing policy

For patch-only, every workspace edit is expressed as apply_patch.

For replace-first, use this selection matrix:

| Edit intent | Preferred tool | Reason |
| --- | --- | --- |
| Localized change to an existing regular UTF-8 file with an exact unique anchor | replace_text | Avoid model-generated diff hunk metadata |
| Create a file | apply_patch | replace_text supports existing files only |
| Delete a file | apply_patch | replace_text cannot delete files |
| Rename a file | apply_patch when the patch tool supports the operation; otherwise report unsupported | Do not simulate a rename with text replacement |
| Large structural rewrite | apply_patch | An exact replacement would reproduce too much source |
| Exact anchor would be impractically large | apply_patch | Keep replacement requests bounded |
| Target is unsupported by replace_text but valid for apply_patch | apply_patch | Use the tool whose contract covers the operation |
| Exact uniqueness is unknown | read_file first | Do not guess between locations |

Localized, large, and impractically large are model judgments guided by the
prompt. The tools remain the deterministic safety boundary: they validate
paths, hashes, exact matches, patch syntax, context, and transactionality.

Issue #10 lists renames as an apply_patch case, while the current patch contract
from Issue #8 rejects rename metadata. Strategy routing may select the patch
path, but Issue #10 must not silently expand the patch tool contract; until
rename support is added separately, the operation fails as unsupported.

### 4.3 Prompt contract

The patch-only prompt instructs the model to:

- read current files before editing;
- use apply_patch for all workspace edits;
- regenerate a patch from fresh content after a structured failure.

The replace-first prompt instructs the model to:

- prefer replace_text only for localized exact unique replacement;
- use apply_patch directly for creation, deletion, structural edits, or
  unsupported replacement targets;
- observe the recovery matrix in Section 6;
- never propose a same-turn fallback after replace_text;
- re-read when an error says current source context is required.

Prompt guidance influences routing but is not treated as a safety guarantee.

## 5. Agent-turn control algorithm

The existing model → planner → executor → observation loop remains in place.
Issue #10 adds only the strategy selection and edit isolation described below.

### 5.1 Batch preflight

Before tool execution, count replace_text and apply_patch calls in the assistant
response.

If more than one editing operation is present:

- reject the complete tool-call batch using the existing Planner/Executor
  rejection path;
- return a structured multiple_edit_operations result for every call;
- leave workspace and verification state unchanged;
- let the next model turn choose one operation after observing the rejection.

This prevents a response such as:

~~~text
replace_text
apply_patch
~~~

from acting as a precomputed fallback. The patch was generated before the model
knew whether the replacement had failed.

### 5.2 Failure observation barrier

When the single editing operation fails:

1. Preserve its error_code, human-readable error, and bounded details.
2. Rely on the one-edit-per-turn preflight to ensure that no later editing
   operation exists in that assistant response.
3. Append the assistant message and one result for every tool call.
4. Send those observations in the next model request.
5. Permit a later edit only after the error-specific read requirement has been
   satisfied.

Every model tool call must still receive a result. A rejected batch must not
leave the provider conversation with an unmatched tool_call.

### 5.3 Recovery read visibility

Some errors require a fresh read before the next edit. The minimal controller
only needs to remember:

- the failed step;
- the error code;
- affected paths;
- whether a required read result has become visible to the model.

No automatic read is required. The model requests read_file in the next turn,
and Yada returns the normal bounded content and current SHA.

If an error occurs in step N:

- a read already visible before step N is not post-failure evidence;
- a read proposed in step N + 1 becomes visible to the model in step N + 2;
- an edit also proposed in step N + 1 was generated without seeing that read
  and causes that complete batch to be rejected when fresh context is required;
- an edit proposed in step N + 2 may use the read result.

Consequently, recovery that requires fresh content uses a read-only Agent turn
followed by an editing turn. This fits the existing whole-batch rejection path
and avoids introducing per-call scheduling.

This is the semantic purpose previously represented by
available_to_model_at_step. It should be implemented with the smallest state
that fits the existing Planner and Executor boundaries.

### 5.4 Successful edit

Successful replace_text and apply_patch calls continue to use the existing tool
state:

- increment workspace revision;
- update touched files;
- invalidate the verified revision;
- require a relevant successful test or build before finish.

A successful edit clears the pending recovery requirement and resets the
per-revision edit-failure counter.

## 6. Recovery matrix

Recovery is model-driven and occurs in later Agent turns. Yada may enforce a
required post-failure read, but it does not generate a replacement or patch.

| Error code | Fresh read required before another edit? | Next model action | Exhaustion behavior |
| --- | --- | --- | --- |
| stale_hash | Yes | Read affected files and reconstruct the edit with current SHA and content. Do not fall back automatically. | Count the failed edit; the per-revision or max_steps boundary eventually ends repeated races. |
| no_match | Yes | Read relevant content, then use current exact text or deliberately generate a new patch. | No forced second-attempt transition; further failures consume the shared edit-failure limit. |
| ambiguous_match | Yes | Read a narrower range or enlarge the exact anchor until the target is unique. | No fixed one-retry limit. Continue only while shared budgets remain; ambiguity alone never authorizes a blind patch. |
| invalid_edit | No | Correct the arguments in a later turn. | Repeated failures consume the shared edit-failure limit. |
| unsupported_target | No, unless current source is needed to build a patch | Use apply_patch only if that operation is valid under its contract; otherwise report the unsupported operation. | No hidden conversion or mutation. |
| invalid_patch | No for syntax-only errors; read if source context may be stale | Correct or regenerate the unified diff. | Repeated failures consume the shared edit-failure limit. |
| patch_context_mismatch | Yes | Read affected files and regenerate the patch from current content. | Repeated failures consume the shared edit-failure limit. |
| apply_failed | No automatic recovery | Preserve diagnostics and fail loudly; the model may inspect the cause, but Yada performs no fallback edit. | The shared boundaries end repeated attempts. |

The invalid_patch row comes from Issue #8, on which Issue #10 depends.

### 6.1 Clarification for ambiguous_match

Issue #10 says to read a narrower range or enlarge the exact anchor until it is
unique. It does not specify that only one corrected replacement is allowed.

Therefore the first implementation must not:

- force PATCH_REQUIRED after one ambiguous retry;
- terminate as unresolved_ambiguity after one retry;
- treat ambiguity itself as permission to patch an uncertain target.

The model may make multiple evidence-based attempts, bounded by the shared
per-revision failure limit and max_steps. A deliberate patch is acceptable only
after fresh context makes the intended target unambiguous; it is never an
automatic reaction to ambiguous_match.

## 7. Loop prevention

### 7.1 Two boundaries

The first implementation uses only two loop boundaries:

1. max_steps, which already decreases on every model turn and guarantees that
   the run is finite;
2. MAX_EDIT_FAILURES_PER_REVISION, initially 4, which ends repeated failed
   editing attempts earlier at one unchanged workspace revision.

The per-revision counter:

- increments when replace_text or apply_patch actually executes and fails;
- does not increment for read-only calls;
- resets after a successful edit advances the revision;
- ends the run as unfinished with edit_failure_budget_exhausted when it reaches
  the configured limit.

Rejected protocol calls can rely on max_steps in the first implementation.
There is no separate protocol-violation budget, no no-progress budget, and no
per-error retry budget.

### 7.2 What these boundaries guarantee

They guarantee finite execution, not successful completion:

- patch-only may repeatedly generate invalid patches;
- replace-first may repeatedly produce no_match or ambiguous_match;
- the model may ignore a requested read;
- the model may avoid editing entirely.

Actual failed edits are stopped early by the per-revision limit. Other
non-progress behavior is stopped by max_steps.

Additional counters should be introduced only after traces or the Issue #10
benchmark demonstrate a loop pattern that these two boundaries cannot diagnose
or control adequately.

No formal budget-vector proof is necessary: max_steps alone is already a
strictly decreasing global bound.

## 8. Control flows

### 8.1 Overall flow

~~~mermaid
flowchart TD
    A["Start run"] --> B{"Editing strategy"}
    B -->|"patch-only"| C["Freeze prompt and tools: apply_patch"]
    B -->|"replace-first"| D["Freeze prompt and tools: replace_text + apply_patch"]
    C --> E["Request model turn"]
    D --> E

    E --> F["Planner inspects proposed tool calls"]
    F --> G{"More than one editing operation?"}
    G -->|"Yes"| H["Reject complete batch with no side effects"]
    H --> I["Return structured results in next model request"]
    I --> N{"max_steps exhausted?"}

    G -->|"No"| J{"Required read pending and edit proposed?"}
    J -->|"Yes"| K["Reject complete batch and request read-only turn"]
    K --> I
    J -->|"No"| L["Execute tools in existing order"]

    L --> M{"Editing result"}
    M -->|"Success"| O["Advance revision and require verification"]
    O --> P["Continue normal Agent loop"]
    P --> N

    M -->|"Failure"| Q["Return structured error; increment revision failure count"]
    Q --> R{"Failure limit reached?"}
    R -->|"Yes"| S["End unfinished: edit_failure_budget_exhausted"]
    R -->|"No"| T["Record any required post-failure read"]
    T --> I

    M -->|"No edit"| P
    N -->|"No"| E
    N -->|"Yes"| U["End unfinished: max_steps_exhausted"]
~~~

### 8.2 Replace-first routing and recovery

~~~mermaid
flowchart TD
    A["Model evaluates edit intent"] --> B{"Localized existing text with exact unique anchor?"}
    B -->|"Yes"| C["Propose replace_text"]
    B -->|"No"| D["Propose apply_patch"]

    C --> E{"replace_text result"}
    E -->|"Success"| F["Verify latest revision"]
    E -->|"stale_hash or no_match"| G["Next turn: read current relevant content"]
    E -->|"ambiguous_match"| H["Next turn: read narrower range or match windows"]
    E -->|"invalid_edit"| I["Next turn: correct arguments"]
    E -->|"unsupported_target"| J["Next turn: choose apply_patch only if valid"]
    E -->|"apply_failed"| K["Preserve diagnostics; no automatic fallback"]

    G --> L["Following turn: corrected replace or deliberate patch"]
    H --> M["Following turn: use a larger unique anchor"]
    M --> E
    I --> C
    J --> D
    L --> N{"Chosen tool"}
    N -->|"replace_text"| C
    N -->|"apply_patch"| D

    D --> O{"apply_patch result"}
    O -->|"Success"| F
    O -->|"stale_hash or patch_context_mismatch"| P["Next turn: read affected files"]
    O -->|"invalid_patch"| Q["Next turn: correct or regenerate patch"]
    O -->|"apply_failed"| K
    P --> R["Following turn: regenerate patch"]
    Q --> R
    R --> D

    E -->|"Any repeated failure"| S["Consume shared per-revision failure limit"]
    O -->|"Any repeated failure"| S
    S --> T{"Limit or max_steps reached?"}
    T -->|"No"| A
    T -->|"Yes"| U["End unfinished with explicit reason"]
~~~

### 8.3 Patch-only behavior

patch-only follows the same recovery matrix but never exposes replace_text:

~~~text
read current source
    → propose apply_patch
    → success: verify
    → structured failure: observe it in the next model turn
    → perform any required fresh read
    → regenerate the patch in a later turn
    → stop at the per-revision failure limit or max_steps
~~~

There is no patch-specific retry counter. A valid patch may succeed on any
attempt before the shared boundaries are reached.

## 9. Trace requirements

The existing tool_call and tool_result events already record:

- selected tool;
- arguments;
- success or failure;
- structured error_code;
- Agent step and tool-call correlation.

Issue #10 adds editing_strategy and the frozen tool names to run_start. It also
needs a bounded trace record when Yada rejects an edit because:

- multiple editing operations were proposed in one turn;
- a required read was not yet visible to the model;
- the per-revision failure limit was exhausted.

The recovery path can then be reconstructed from existing ordered model, call,
and result events. A large family of recovery_started, recovery_read, and
recovery_transition events is not required unless implementation experience
shows that existing traces are insufficient.

An unfinished run records a stable terminal reason such as:

- edit_failure_budget_exhausted;
- max_steps_exhausted;
- an existing fatal tool or runtime error.

## 10. Deterministic tests

Mocked model and tool interactions should cover:

### Strategy stability

- patch-only exposes apply_patch but not replace_text;
- replace-first exposes both editing tools;
- strategy and schemas remain stable across all model requests;
- run_start records strategy, tool names, and the edit-failure limit.

### Routing

- a localized existing-file change is prompted toward replace_text;
- creation, deletion, and structural edits are prompted toward apply_patch;
- patch-only never exposes replace_text.

### Turn isolation and visibility

- a turn with replace_text and apply_patch executes neither edit;
- a failed edit appears in the next model request;
- every call in a rejected batch receives a tool result;
- a read and edit generated in the same recovery turn cause the batch to be
  rejected and cannot satisfy the fresh-read requirement;
- an edit in the following turn can use the now-visible read.

### Recovery

- stale_hash requires a fresh read before retry;
- no_match permits a corrected replacement or deliberate patch after a read;
- repeated ambiguous_match can continue with larger anchors while shared
  budgets remain;
- ambiguity never causes automatic patch fallback;
- invalid_edit permits corrected arguments in a later turn;
- unsupported_target permits patch only when the patch contract supports it;
- invalid_patch permits a regenerated patch;
- patch_context_mismatch requires a fresh read;
- apply_failed preserves diagnostics and performs no hidden edit.

### Loop and safety boundaries

- four failed edits at one revision end with
  edit_failure_budget_exhausted;
- a successful edit resets the per-revision failure counter;
- text-only, repeated-read, or rejected-call loops still end at max_steps;
- failed and rejected edits leave files, revision, touched paths, and
  verification unchanged;
- successful fallback remains SHA-bound, transactional, and subject to
  post-edit verification.

## 11. Evaluation

Compare patch-only and replace-first using:

- the same task set and base commits;
- the same model and model parameters;
- the same step, token, command, and wall-time budgets;
- stable prompts and tool schemas within each strategy;
- repeated runs when model nondeterminism requires them.

Record at least:

- first edit-attempt success rate;
- eventual mutation success rate;
- completed or resolved task rate;
- edit retries;
- Agent turns and total steps;
- input and output tokens;
- verification success after mutation;
- unrelated changed lines;
- wrong-target or partial-target mutations, which must remain zero;
- terminal reason distribution.

The first benchmark should run with max_steps and
MAX_EDIT_FAILURES_PER_REVISION only. Add more specialized retry controls only
when a measured failure mode justifies them.

## 12. Implementation guidance

Keep the implementation within the existing Planner, Executor, ToolRunner,
prompt, trace, CLI, and evaluation boundaries described in
[Yada architecture](architecture.md).

The design deliberately does not define a new editing.py state machine or copy
the complete Agent loop into this document. Source code and tests are the
authoritative description of implementation mechanics. After Issue #10 lands,
this section should link to the small policy functions and their tests rather
than duplicate them.

replace_text and apply_patch remain data-plane tools. Neither contains routing
policy or invokes the other as a strategy fallback.

## 13. Industry context

Exact unique replacement, optimistic concurrency checks, fail-closed
transactions, structured errors, and trace-based evaluation are established
patterns rather than one universal editing algorithm. Comparable examples
include [Gemini CLI file tools](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/file-system.md),
[Claude text editor](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool),
and HTTP [If-Match](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.1).

Yada keeps only the parts needed to test Issue #10 without turning the harness
into a general workflow engine.
