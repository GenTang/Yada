"""Stable DeepSeek function schemas for Yada's model-facing tools."""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "select_strategy",
            "description": (
                "Select the irreversible Host-enforced verification strategy. "
                "Read-only inspection is allowed first, but this tool must succeed "
                "before any file edit. Call it alone in its assistant turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["red_green", "direct_execute"],
                        "description": (
                            "Use red_green when a focused test can be authored to fail "
                            "on baseline behavior, even if no failing test exists yet. "
                            "Use direct_execute only when meaningful baseline failure "
                            "is inapplicable."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Concise evidence-based reason for the selection.",
                    },
                },
                "required": ["strategy", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search a regular expression in workspace files when the target location is unclear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Regular expression understood by ripgrep/Python re; "
                            "escape regex metacharacters when searching for literal text."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path; default '.'.",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "Optional glob such as '*.py'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matches, 1-200.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read up to 400 numbered lines and return the current SHA-256 for safe editing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to an existing file.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "One-based first line, inclusive; default 1.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": (
                            "One-based final line, inclusive; defaults to a 200-line "
                            "window from start_line. At most 400 lines may be read."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply a git-style unified diff transactionally. Hunk old/new line "
                "counts are recalculated automatically and need not be exact, but "
                "headers must be valid and context must match. expected_files must "
                "list exactly every patch target with no extra or missing paths; "
                "existing files use their read_file SHA-256 and new files use NEW."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Unified diff with one diff --git header per target.",
                    },
                    "expected_files": {
                        "type": "array",
                        "description": (
                            "Exact set of files touched by patch, with one entry per "
                            "target."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Workspace-relative patch target path.",
                                },
                                "sha256": {
                                    "type": "string",
                                    "description": (
                                        "Current read_file SHA-256 for an existing "
                                        "file, or NEW for a new file."
                                    ),
                                },
                            },
                            "required": ["path", "sha256"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["patch", "expected_files"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": (
                "Replace exact unique text in existing UTF-8 files as one SHA-bound "
                "transaction. Cannot create or delete files. Edits to the same file "
                "run in declared order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": (
                            "One to 100 exact replacements applied as one transaction."
                        ),
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Workspace-relative path to an existing regular UTF-8 file.",
                                },
                                "sha256": {
                                    "type": "string",
                                    "description": (
                                        "SHA-256 from read_file; all edits for one "
                                        "file use the same starting hash."
                                    ),
                                },
                                "old_text": {
                                    "type": "string",
                                    "description": (
                                        "Exact non-empty Unicode text, including "
                                        "whitespace and line breaks; must match exactly "
                                        "once at this point in the ordered transaction."
                                    ),
                                },
                                "new_text": {
                                    "type": "string",
                                    "description": "Literal replacement text; may be empty.",
                                },
                            },
                            "required": [
                                "path",
                                "sha256",
                                "old_text",
                                "new_text",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["edits"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_red_test",
            "description": (
                "Run and submit the exact target test for Host validation against "
                "baseline production plus the current test-only patch. A valid "
                "behavioral failure freezes the test and ends the Red session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Exact target test identity, also present as one argv item; "
                            "for example tests/test_api.py::test_regression."
                        ),
                    },
                    "argv": {
                        "type": "array",
                        "description": "Exact target-test command as separate argv strings.",
                        "items": {"type": "string"},
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Workspace-relative command directory; default '.'.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout from 1 to 1800 seconds.",
                    },
                },
                "required": ["target", "argv"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an argv array without a shell for inspection, testing, or builds; do not modify workspace files. Prefer direct test/build commands; wrappers must propagate child exit status. Commands require policy approval unless Yada runs with --yes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "description": "Executable and arguments as separate strings; no shell parsing.",
                        "items": {"type": "string"},
                    },
                    "purpose": {
                        "type": "string",
                        "enum": ["inspect", "test", "build"],
                        "description": (
                            "Command intent; only a successful test or build satisfies "
                            "the verification gate."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Workspace-relative working directory; default '.'.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "Timeout from 1 to 1800 seconds; a timeout is returned as "
                            "a structured non-verification result."
                        ),
                    },
                    "verification_role": {
                        "type": "string",
                        "enum": ["target", "regression"],
                        "description": (
                            "During the Fix phase, mark the exact frozen command as "
                            "target or a distinct broader check as regression. Omit for "
                            "direct_execute and ordinary inspection."
                        ),
                    },
                },
                "required": ["argv", "purpose"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Submit the completed task. Rejected unless a patch exists and a relevant test/build passed after the latest patch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Concise description of the completed change.",
                    }
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]
