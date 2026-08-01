"""Bounded, hash-producing file reads."""

from __future__ import annotations

from typing import Any

from yada.exceptions import ToolError
from yada.tools.base import ToolContext


def read_file(
    context: ToolContext,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    file_path = context.workspace.resolve(path)
    if not file_path.is_file():
        raise ToolError(f"not a file: {path}")
    if file_path.stat().st_size > 1_000_000:
        raise ToolError("file is larger than the 1 MB read limit")
    if not isinstance(start_line, int) or start_line < 1:
        raise ToolError("start_line must be a positive integer")
    if end_line is None:
        end_line = start_line + 199
    if not isinstance(end_line, int) or end_line < start_line:
        raise ToolError("end_line must be greater than or equal to start_line")
    if end_line - start_line + 1 > 400:
        raise ToolError("a single read is limited to 400 lines")

    text = file_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    selected = lines[start_line - 1 : end_line]
    numbered = "\n".join(
        f"{number:>6}|{line}"
        for number, line in enumerate(selected, start=start_line)
    )
    return {
        "path": context.workspace.display(file_path),
        "sha256": context.workspace.sha256(file_path),
        "start_line": start_line,
        "end_line": start_line + len(selected) - 1 if selected else start_line - 1,
        "total_lines": len(lines),
        "content": numbered,
    }

