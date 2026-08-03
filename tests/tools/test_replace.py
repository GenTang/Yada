from __future__ import annotations

import json
from pathlib import Path

from yada.agents.executor import Executor
from yada.exceptions import ToolError
from yada.tools import ToolRunner
from yada.traces import TraceWriter, read_trace


def _edit(
    tool_runner: ToolRunner,
    path: str,
    old_text: str,
    new_text: str,
    *,
    sha256: str | None = None,
) -> dict[str, str]:
    file_path = tool_runner.workspace.resolve(path)
    return {
        "path": path,
        "sha256": sha256 or tool_runner.workspace.sha256(file_path),
        "old_text": old_text,
        "new_text": new_text,
    }


def test_replace_text_is_public_and_updates_edit_state(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    names = [schema["function"]["name"] for schema in tool_runner.schemas]
    assert "replace_text" in names
    tool_runner.context.state.verified_revision = 0

    result = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "app.py", "return 41", "return 42")]},
    )

    assert result.data["ok"], result.data
    assert (git_workspace / "app.py").read_text() == "def answer():\n    return 42\n"
    assert result.data["changed_files"] == [
        {
            "path": "app.py",
            "sha256": tool_runner.workspace.sha256(git_workspace / "app.py"),
        }
    ]
    assert tool_runner.context.state.revision == 1
    assert tool_runner.context.state.patch_count == 1
    assert tool_runner.context.state.touched_files == {"app.py"}
    assert tool_runner.context.state.verified_revision == -1
    assert not tool_runner.execute("finish", {"summary": "done"}).data["ok"]


def test_no_match_is_structured_and_does_not_change_state(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    tool_runner.context.state.verified_revision = 0

    result = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "app.py", "return 99", "return 42")]},
    )

    assert result.data["error_code"] == "no_match"
    assert result.data["details"]["paths"] == ["app.py"]
    assert "recovery" in result.data["details"]
    assert (git_workspace / "app.py").read_text() == "def answer():\n    return 41\n"
    assert tool_runner.context.state.revision == 0
    assert tool_runner.context.state.patch_count == 0
    assert tool_runner.context.state.verified_revision == 0


def test_overlapping_matches_are_ambiguous(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    repeated = git_workspace / "repeated.txt"
    repeated.write_text("aaaa\n", encoding="utf-8")

    result = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "repeated.txt", "aa", "b")]},
    )

    assert result.data["error_code"] == "ambiguous_match"
    assert result.data["details"]["match_lines"] == [1]
    assert repeated.read_text() == "aaaa\n"


def test_stale_hash_reports_current_hash(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    result = tool_runner.execute(
        "replace_text",
        {
            "edits": [
                _edit(
                    tool_runner,
                    "app.py",
                    "return 41",
                    "return 42",
                    sha256="0" * 64,
                )
            ]
        },
    )

    assert result.data["error_code"] == "stale_hash"
    assert result.data["details"]["current_sha256"] == tool_runner.workspace.sha256(
        git_workspace / "app.py"
    )
    assert "return 41" in (git_workspace / "app.py").read_text()


def test_same_file_edits_run_in_order_against_memory(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    target = git_workspace / "ordered.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    digest = tool_runner.workspace.sha256(target)

    result = tool_runner.execute(
        "replace_text",
        {
            "edits": [
                _edit(
                    tool_runner,
                    "ordered.txt",
                    "alpha",
                    "gamma",
                    sha256=digest,
                ),
                _edit(
                    tool_runner,
                    "ordered.txt",
                    "gamma beta",
                    "done",
                    sha256=digest,
                ),
            ]
        },
    )

    assert result.data["ok"], result.data
    assert target.read_text() == "done\n"


def test_same_file_edits_require_one_starting_hash(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")
    result = tool_runner.execute(
        "replace_text",
        {
            "edits": [
                _edit(
                    tool_runner,
                    "app.py",
                    "return 41",
                    "return 42",
                    sha256=digest,
                ),
                _edit(
                    tool_runner,
                    "app.py",
                    "return 42",
                    "return 43",
                    sha256="0" * 64,
                ),
            ]
        },
    )

    assert result.data["error_code"] == "invalid_edit"
    assert "return 41" in (git_workspace / "app.py").read_text()


def test_later_file_failure_rolls_back_every_edit(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    second = git_workspace / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")

    result = tool_runner.execute(
        "replace_text",
        {
            "edits": [
                _edit(tool_runner, "app.py", "return 41", "return 42"),
                _edit(tool_runner, "second.py", "VALUE = 9", "VALUE = 2"),
            ]
        },
    )

    assert result.data["error_code"] == "no_match"
    assert (git_workspace / "app.py").read_text() == "def answer():\n    return 41\n"
    assert second.read_text() == "VALUE = 1\n"
    assert tool_runner.context.state.revision == 0


def test_unsupported_text_targets_are_rejected(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    (git_workspace / "folder").mkdir()
    (git_workspace / "link.py").symlink_to("app.py")
    binary = git_workspace / "binary.dat"
    binary.write_bytes(b"valid utf-8\0binary")
    invalid_utf8 = git_workspace / "invalid.dat"
    invalid_utf8.write_bytes(b"\xff\xfe")
    oversized = git_workspace / "oversized.txt"
    oversized.write_bytes(b"x" * 1_000_001)
    cases = [
        ("missing.py", "0" * 64),
        ("folder", "0" * 64),
        ("link.py", "0" * 64),
        ("binary.dat", tool_runner.workspace.sha256(binary)),
        ("invalid.dat", tool_runner.workspace.sha256(invalid_utf8)),
        ("oversized.txt", tool_runner.workspace.sha256(oversized)),
        (".git/config", "0" * 64),
        ("../outside.py", "0" * 64),
    ]

    for path, digest in cases:
        result = tool_runner.execute(
            "replace_text",
            {
                "edits": [
                    {
                        "path": path,
                        "sha256": digest,
                        "old_text": "old",
                        "new_text": "new",
                    }
                ]
            },
        )
        assert result.data["error_code"] == "unsupported_target", result.data

    assert tool_runner.context.state.revision == 0
    assert "return 41" in (git_workspace / "app.py").read_text()


def test_invalid_and_oversized_edits_are_bounded(
    tool_runner: ToolRunner,
) -> None:
    empty = tool_runner.execute("replace_text", {"edits": []})
    unchanged = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "app.py", "return 41", "return 41")]},
    )
    oversized = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "app.py", "return 41", "x" * 100_001)]},
    )

    assert empty.data["error_code"] == "invalid_edit"
    assert unchanged.data["error_code"] == "invalid_edit"
    assert oversized.data["error_code"] == "invalid_edit"
    assert len(oversized.content) < 4_000


def test_whole_file_can_be_replaced_with_empty_text(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    target = git_workspace / "empty-me.txt"
    target.write_bytes(b"no final newline")

    result = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "empty-me.txt", "no final newline", "")]},
    )

    assert result.data["ok"], result.data
    assert target.read_bytes() == b""


def test_unicode_crlf_empty_replacement_and_missing_final_newline(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    crlf = git_workspace / "你好.txt"
    crlf.write_bytes("first\r\nsecond\r\n".encode())
    no_newline = git_workspace / "no newline.txt"
    no_newline.write_bytes(b"alpha omega")

    result = tool_runner.execute(
        "replace_text",
        {
            "edits": [
                _edit(tool_runner, "你好.txt", "second", "第二"),
                _edit(tool_runner, "no newline.txt", " omega", ""),
            ]
        },
    )

    assert result.data["ok"], result.data
    assert crlf.read_bytes() == "first\r\n第二\r\n".encode()
    assert no_newline.read_bytes() == b"alpha"


def test_cancelling_edits_are_rejected_without_mutation(
    git_workspace: Path, tool_runner: ToolRunner
) -> None:
    digest = tool_runner.workspace.sha256(git_workspace / "app.py")
    result = tool_runner.execute(
        "replace_text",
        {
            "edits": [
                _edit(
                    tool_runner,
                    "app.py",
                    "return 41",
                    "return 42",
                    sha256=digest,
                ),
                _edit(
                    tool_runner,
                    "app.py",
                    "return 42",
                    "return 41",
                    sha256=digest,
                ),
            ]
        },
    )

    assert result.data["error_code"] == "invalid_edit"
    assert "return 41" in (git_workspace / "app.py").read_text()


def test_generated_patch_failure_is_reported_as_apply_failed(
    monkeypatch, git_workspace: Path, tool_runner: ToolRunner
) -> None:
    def reject_patch(*args, **kwargs):
        raise ToolError(
            "generated patch did not apply",
            error_code="patch_context_mismatch",
            details={"phase": "check", "paths": ["app.py"]},
        )

    monkeypatch.setattr("yada.tools.replace.apply_patch", reject_patch)

    result = tool_runner.execute(
        "replace_text",
        {"edits": [_edit(tool_runner, "app.py", "return 41", "return 42")]},
    )

    assert result.data["error_code"] == "apply_failed"
    assert result.data["details"]["cause_error_code"] == ("patch_context_mismatch")
    assert "return 41" in (git_workspace / "app.py").read_text()
    assert tool_runner.context.state.revision == 0


def test_replace_text_execution_is_auditable(
    tmp_path: Path, tool_runner: ToolRunner
) -> None:
    trace_path = tmp_path / "replace-trace.jsonl"
    executor = Executor(
        tools=tool_runner,
        trace=TraceWriter(trace_path, level="debug"),
        emit=lambda _: None,
    )
    arguments = {"edits": [_edit(tool_runner, "app.py", "return 41", "return 42")]}
    call = {
        "id": "replace-1",
        "type": "function",
        "function": {
            "name": "replace_text",
            "arguments": json.dumps(arguments),
        },
    }

    executed = executor.execute_batch(1, (call,))
    events = read_trace(trace_path)

    assert executed[0].execution.data["ok"]
    assert [event["data"]["tool"] for event in events] == [
        "replace_text",
        "replace_text",
    ]
    assert events[-1]["data"]["result"]["ok"]
