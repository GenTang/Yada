"""Small, failure-tolerant helpers for trace reproducibility metadata."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from yada import __version__


def collect_provenance(
    workspace: Path,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stable local versions without making tracing a run dependency."""

    values: dict[str, Any] = {
        "yada_version": __version__,
        "yada_commit": _yada_commit(),
        "workspace_base_commit": git_head(workspace),
    }
    values.update(extra or {})
    return values


def client_trace_config(client: object) -> dict[str, Any]:
    """Return a provider-safe model configuration when the client exposes one."""

    config = getattr(client, "trace_config", None)
    if callable(config):
        value = config()
        if isinstance(value, dict):
            return value
    return {"model": str(getattr(client, "model", "unknown"))}


def git_head(path: Path) -> str | None:
    """Read a Git HEAD if available, returning ``None`` for non-repositories."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _yada_commit() -> str | None:
    source = Path(__file__).resolve()
    for parent in source.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src/yada").is_dir():
            return git_head(parent)
    return None
