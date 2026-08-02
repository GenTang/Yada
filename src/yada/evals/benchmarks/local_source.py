"""Git source preparation for portable local benchmark cases."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def prepare_source(
    value: object,
    *,
    base_dir: Path,
    cache_root: Path,
    base_commit: object,
) -> Path:
    """Resolve a local path or populate an exact cached Git checkout."""

    if isinstance(value, str):
        if not value.strip():
            raise ValueError("manifest workspace must be a non-empty string")
        return _resolve_path(base_dir, value)
    if not isinstance(value, dict) or value.get("type") != "git":
        raise ValueError("manifest workspace must be a path or a git workspace")

    url = _required_string(value, "url")
    cache_key = _safe_cache_key(_required_string(value, "cache_key"))
    expected = str(value.get("base_commit") or base_commit or "").strip()
    if not expected:
        raise ValueError("git workspace requires base_commit")
    source = cache_root / cache_key / "repo"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        source.mkdir()
        commands = [
            ["git", "init", "--quiet"],
            ["git", "remote", "add", "origin", url],
            ["git", "fetch", "--quiet", "--depth", "1", "origin", expected],
            ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ]
        for argv in commands:
            result = _run(argv, source, timeout=600)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f"failed command: {argv}")
    if not (source / ".git").exists():
        raise ValueError(f"benchmark cache is not a Git repository: {source}")
    remote = _run(["git", "remote", "get-url", "origin"], source)
    if remote.returncode or remote.stdout.strip() != url:
        raise ValueError(f"benchmark cache origin does not match recipe: {source}")
    head = _run(["git", "rev-parse", "HEAD"], source)
    if head.returncode or head.stdout.strip() != expected:
        require_clean(source)
        fetch = _run(
            ["git", "fetch", "--quiet", "--depth", "1", "origin", expected],
            source,
            timeout=600,
        )
        if fetch.returncode:
            raise RuntimeError(fetch.stderr.strip() or "Git fetch failed")
        checkout = _run(
            ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            source,
        )
        if checkout.returncode:
            raise RuntimeError(checkout.stderr.strip() or "Git checkout failed")
    require_head(source, expected)
    require_clean(source)
    return source


def require_head(workspace: Path, expected: str) -> None:
    result = _run(["git", "rev-parse", "HEAD"], workspace)
    actual = result.stdout.strip()
    if result.returncode or actual != expected:
        raise ValueError(f"expected base commit {expected}, got {actual or 'unknown'}")


def require_clean(workspace: Path) -> None:
    result = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        workspace,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "cannot inspect workspace status")
    if result.stdout.strip():
        raise ValueError("local source workspace must be clean before copy mode")


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"workspace {key} must be a non-empty string")
    return value


def _safe_cache_key(value: str) -> Path:
    key = Path(value)
    if (
        key.is_absolute()
        or not key.parts
        or any(part in {"", ".", ".."} for part in key.parts)
    ):
        raise ValueError("workspace cache_key must be a safe relative path")
    return key


def _run(
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
