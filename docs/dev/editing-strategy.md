# Editing Strategies

## Status

This document defines the minimal implementation for
[Issue #10: replace-first routing with apply_patch fallback](https://github.com/GenTang/Yada/issues/10).

Issues #8 and #9 make apply_patch and replace_text independently safe. Issue
#10 adds the thin coordination layer between them:

- a stable run-level strategy;
- explicit routing and recovery instructions;
- one deterministic host rule: one editing operation per Agent turn;
- trace and evaluation data that make the strategies comparable.

It does not add a recovery state machine, automatic recovery reads, per-error
retry budgets, or hidden tool conversion.

## 1. Responsibilities

### 1.1 Editing tools

The tools remain responsible for their existing contracts:

- exact SHA validation;
- path and target validation;
- fail-closed matching or patch application;
- transactional multi-file mutation;
- structured, bounded errors;
- revision and verification bookkeeping.

Neither tool owns routing policy. A failed replace_text call never constructs or
executes an apply_patch fallback.

### 1.2 Model

The model is responsible for:

- deciding whether the intended edit is localized or structural;
- selecting an editing tool according to the strategy prompt;
- observing structured errors;
- re-reading when the recovery policy requires current content;
- proposing a corrected replacement or patch in a later turn;
- verifying the final revision before calling finish_task.

### 1.3 Yada host

Yada is responsible for:

- selecting and freezing the run strategy;
- freezing the strategy-specific prompt and tool schemas;
- rejecting an Assistant response containing more than one editing operation;
- returning every tool failure to the model through the existing message loop;
- recording strategy, tool selection, results, and errors;
- stopping the run at the existing max_steps boundary.

## 2. Run-level strategies

Yada supports:

| Strategy | Exposed editing tools | Prompt policy |
| --- | --- | --- |
| patch-only | apply_patch | Express every workspace edit as a checked unified diff. |
| replace-first | replace_text and apply_patch | Prefer exact replacement for suitable localized edits and patch otherwise. |

replace-first is the default. patch-only remains available as an explicit baseline
and for runs that require unified-diff-only editing.

The strategy is selected once through:

~~~text
--editing-strategy patch-only
--editing-strategy replace-first
~~~

The ToolRunner builds its handlers and schemas once. The Planner builds its
system prompt once from the same strategy. Neither changes during the run.

The run_start trace records:

~~~json
{
  "editing_strategy": "replace-first",
  "tool_names": [
    "search_code",
    "read_file",
    "apply_patch",
    "replace_text",
    "run_command",
    "finish_task"
  ]
}
~~~

## 3. Replace-first routing

Use replace_text when all of these are true:

- the target is an existing regular UTF-8 text file;
- the intended change is localized;
- current source text supplies an exact, unique anchor;
- the anchor is reasonably bounded.

Use apply_patch directly when:

- creating or deleting a file;
- applying a large structural rewrite;
- the exact anchor would reproduce an impractically large source block;
- replace_text does not support the target operation.

Issue #10 also names renames as a patch case. The current Issue #8 patch
contract rejects rename metadata, so routing may select apply_patch but the
operation remains unsupported until rename support is added separately.

The semantic predicates such as localized are model judgments. Yada does not
add an AST router or second model. Tool validation remains the deterministic
safety boundary.

## 4. Recovery policy

Fallback means a deliberate apply_patch call in a later Agent turn after the
model has observed a replace_text failure. It does not mean:

- replace_text internally calling apply_patch as a strategy decision;
- Yada converting failed replacement arguments into a patch;
- the model submitting replace_text and apply_patch in the same response.

The detailed recovery matrix is the design and test reference:

| Error code | Required model response |
| --- | --- |
| stale_hash | Re-read the affected file before retrying. Do not fall back automatically. |
| no_match | Re-read relevant content, then use current exact text or deliberately generate a patch. |
| ambiguous_match | Read a narrower range or enlarge the exact anchor until it is unique. |
| invalid_edit | Correct the arguments in a later turn. |
| unsupported_target | Use apply_patch only when its contract supports the requested operation. |
| invalid_patch | Correct or regenerate the patch. |
| patch_context_mismatch | Re-read affected files and regenerate the patch. |
| apply_failed | Preserve and act on the diagnostic evidence; Yada performs no fallback. |

invalid_patch comes from Issue #8, on which Issue #10 depends.

The system prompt tells the model to follow the structured recovery instruction
returned with the actual error, refresh stale content when required, and retry or
switch tools only in a later turn. Keeping the full table here avoids paying for and
repeatedly presenting the same verbose matrix on every model turn.

The host does not enforce a multi-stage recovery protocol. If the model ignores
the prompt, the resulting call is handled by the ordinary tool contract and
remains visible in the trace. Repeated non-progress ends at max_steps.

## 5. One editing operation per turn

Define:

~~~python
EDITING_TOOLS = {"replace_text", "apply_patch"}
~~~

Before execution, the Planner counts editing calls in the Assistant response.
If the count is greater than one, Yada rejects the complete batch:

~~~json
{
  "ok": false,
  "error_code": "multiple_edit_operations",
  "error": "Only one editing operation is allowed per Agent turn."
}
~~~

Every proposed call receives a rejection result so the provider conversation
contains no unmatched tool_call.

This rule matters because all calls in one Assistant response are generated
before any result is visible to the model. A response containing:

~~~text
replace_text
apply_patch
~~~

has no conditional if-replace-fails semantics. The patch is an unconditional,
precomputed second edit, not an evidence-based fallback.

Both editing tools already support transactional multi-file requests. One
editing operation per turn does not mean one file or one changed location per
turn.

## 6. End-to-end algorithm

~~~text
initialize run
    select editing strategy
    build strategy prompt and tool interface once
    record strategy and tool names

for each model turn up to max_steps
    request completion with the frozen prompt and schemas
    parse tool calls

    if more than one editing call is present
        reject the complete batch without side effects
        append one structured result per call
        continue to the next model turn

    execute the accepted calls in their existing order
    append every tool result

    if an edit failed
        the next model turn observes its structured error
        the model follows the prompt recovery matrix

    if verified finish_task succeeds
        end successfully

end unfinished when max_steps is exhausted
~~~

There is no same-call fallback and no host-generated mutation.

## 7. Flowcharts

### 7.1 Overall control flow

~~~mermaid
flowchart TD
    A["Start run"] --> B{"Editing strategy"}
    B -->|"patch-only"| C["Freeze patch-only prompt and schemas"]
    B -->|"replace-first"| D["Freeze replace-first prompt and schemas"]
    C --> E["Request model turn"]
    D --> E

    E --> F["Planner parses tool calls"]
    F --> G{"More than one editing call?"}
    G -->|"Yes"| H["Reject complete batch without side effects"]
    H --> I["Return structured results to next model turn"]
    G -->|"No"| J["Execute accepted calls in order"]

    J --> K{"Edit result"}
    K -->|"Success"| L["Invalidate old verification and continue"]
    K -->|"Failure"| M["Return structured error to next model turn"]
    K -->|"No edit"| N["Continue normal loop"]

    I --> O{"max_steps exhausted?"}
    L --> O
    M --> O
    N --> O
    O -->|"No"| E
    O -->|"Yes"| P["End unfinished"]
~~~

### 7.2 Replace-first fallback

~~~mermaid
flowchart TD
    A["Model evaluates edit"] --> B{"Localized existing text with exact unique anchor?"}
    B -->|"Yes"| C["replace_text"]
    B -->|"No"| D["apply_patch"]

    C --> E{"Result"}
    E -->|"Success"| F["Verify latest revision"]
    E -->|"Failure"| G["Next model turn observes structured error"]
    G --> H{"Recovery guidance"}
    H -->|"Fresh source required"| I["read_file in a later turn"]
    H -->|"Arguments invalid"| J["Correct arguments"]
    H -->|"Patch is now appropriate"| D
    I --> K["Following turn: corrected replace or deliberate patch"]
    J --> C
    K --> C
    K --> D

    D --> L{"Patch result"}
    L -->|"Success"| F
    L -->|"Failure"| M["Next turn: diagnose, re-read if required, regenerate"]
    M --> D
~~~

## 8. Trace and evaluation

Existing tool_call and tool_result events already record:

- chosen editing tool;
- arguments and correlation ID;
- success or failure;
- structured error code and bounded details;
- the later calls that form the recovery path.

Issue #10 adds editing_strategy and frozen tool_names to run_start. Batch
rejection is recorded as a protocol_violation with
multiple_edit_operations.

Native evaluation results also record the strategy and editing metrics:

- first edit-attempt success;
- eventual mutation success;
- edit attempts, additional attempts, and failed attempts;
- replace and patch attempt counts;
- rejected editing calls;
- error-code counts;
- verification success after mutation;
- Agent steps and token usage.

Resolved-task rate comes from the benchmark grader. Unrelated changed lines and
wrong-target or partial-target mutations require benchmark-specific ground
truth and cannot be inferred reliably by the generic Agent loop.

Fair comparison requires the same:

- tasks and base commits;
- model and model parameters;
- step, token, command, and wall-time budgets;
- grading logic;
- number of repeated trials.

Run each task with both:

~~~text
yada eval ... --editing-strategy patch-only
yada eval ... --editing-strategy replace-first
~~~

No benchmark winner is claimed until those controlled runs exist.

## 9. Deterministic tests

Tests cover:

- patch-only exposes apply_patch but not replace_text;
- replace-first exposes both editing tools;
- prompts document the matching routing policy;
- strategy and tool names appear in run_start;
- schemas stay unchanged across model requests;
- two editing calls in one response execute neither call;
- every rejected call receives a structured result;
- a failed replacement is visible before a later patch;
- no tool or host code performs automatic strategy fallback;
- CLI and evaluation adapters propagate the selected strategy;
- existing SHA, transaction, and verification tests pass unchanged.

## 10. Deliberate limits

The first implementation relies on max_steps as its only loop boundary. It does
not add:

- per-error retry limits;
- a per-revision edit-failure budget;
- protocol or no-progress budgets;
- unchanged-attempt fingerprints;
- automatic recovery reads;
- mandatory read state;
- PATCH_REQUIRED or other recovery phases;
- a formal termination proof beyond the existing finite model-turn loop.

If trace-backed benchmarks reveal a concrete repeated-failure pattern, address
that pattern in a separate, measured change.
