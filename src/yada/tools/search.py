"""Repository search tool."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from yada.environments.workspace import PROTECTED_PARTS
from yada.exceptions import ToolError
from yada.tools.base import ToolContext
from yada.utils.text import truncate_text


def search_code(
    context: ToolContext,
    query: str,
    path: str = ".",
    file_glob: str | None = None,
    max_results: int = 80,
) -> dict[str, Any]:
    """Search repository text with ripgrep and a bounded Python fallback.

    Args:
        context: Shared workspace boundary and output limit.
        query: Regular expression understood by ripgrep/Python ``re``.
        path: Workspace-relative file or directory to search.
        file_glob: Optional include/exclude glob forwarded to the search backend.
        max_results: Maximum matching lines returned to the model.

    Returns:
        A bounded, line-and-column-addressed observation.
    """

    if not isinstance(query, str) or not query:
        raise ToolError("query must be a non-empty string")
    if not isinstance(max_results, int) or not 1 <= max_results <= 200:
        raise ToolError("max_results must be between 1 and 200")
    target = context.workspace.resolve(path)
    relative = context.workspace.display(target)
    rg = shutil.which("rg")
    if rg:
        argv = [
            rg,
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
            "--glob",
            "!.git/**",
            "--glob",
            "!.yada/**",
        ]
        if file_glob:
            argv.extend(["--glob", file_glob])
        argv.extend(["--", query, relative])
        try:
            result = subprocess.run(
                argv,
                cwd=context.workspace.root,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("search timed out") from exc
        if result.returncode not in {0, 1}:
            raise ToolError(f"rg failed: {result.stderr.strip()[:1000]}")
        lines = result.stdout.splitlines()[:max_results]
    else:
        # The fallback keeps the core usable on minimal containers, while matching
        # rg's safety properties by skipping protected, binary, and oversized files.
        lines = _python_search(context, query, target, file_glob, max_results)
    text, truncated = truncate_text("\n".join(lines), context.max_output_chars)
    return {
        "query": query,
        "path": relative,
        "matches": text,
        "match_count_returned": len(lines),
        "truncated": truncated,
    }


def _python_search(
    context: ToolContext,
    query: str,
    target: Path,
    file_glob: str | None,
    max_results: int,
) -> list[str]:
    try:
        pattern = re.compile(query)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    files = [target] if target.is_file() else target.rglob("*")
    matches: list[str] = []
    for file_path in files:
        if len(matches) >= max_results:
            break
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(context.workspace.root)
        if any(part in PROTECTED_PARTS for part in relative.parts):
            continue
        if file_glob and not fnmatch.fnmatch(relative.as_posix(), file_glob):
            continue
        try:
            raw = file_path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192] or len(raw) > 2_000_000:
            continue
        for line_number, line in enumerate(
            raw.decode("utf-8", errors="replace").splitlines(), 1
        ):
            match = pattern.search(line)
            if match:
                matches.append(
                    f"{relative.as_posix()}:{line_number}:{match.start() + 1}:{line}"
                )
                if len(matches) >= max_results:
                    break
    return matches
