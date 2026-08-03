"""Stable DeepSeek function schemas for Yada's six tools."""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search text or regex in workspace files. Use this to locate symbols before reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Regex search pattern."},
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
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
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
            "description": "Apply a git-style unified diff transactionally. Every touched existing file needs its read_file SHA-256; new files use NEW.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Unified diff with diff --git headers.",
                    },
                    "expected_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "sha256": {"type": "string"},
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
            "description": "Replace exact unique text in existing UTF-8 files as one SHA-bound transaction. Edits to the same file run in declared order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "sha256": {"type": "string"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
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
            "name": "run_command",
            "description": "Run an argv array in the workspace without a shell. Label it inspect, test, or build. Commands require policy approval unless Yada runs with --yes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "purpose": {
                        "type": "string",
                        "enum": ["inspect", "test", "build"],
                    },
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["argv", "purpose"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Submit the completed task. Rejected unless a patch exists and a relevant test/build passed after the latest patch.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]
