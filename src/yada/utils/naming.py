"""Readable, portable names for local run artifacts."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def task_slug(task: str, *, max_length: int = 48) -> str:
    """Return a short cross-platform filename component derived from a task."""

    if max_length < 1:
        raise ValueError("max_length must be positive")
    normalized = unicodedata.normalize("NFKC", task).casefold().strip()
    slug = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in normalized
    )
    slug = re.sub(r"-+", "-", slug).strip("-_.")
    slug = slug[:max_length].rstrip("-_.") or "task"
    if slug in _WINDOWS_RESERVED_NAMES:
        slug = f"task-{slug}"
    return slug


def readable_run_name(task: str, *, now: datetime | None = None) -> str:
    """Combine a task slug with the system-local time at minute precision."""

    instant = now or datetime.now().astimezone()
    timestamp = instant.strftime("%Y-%m-%d_%H-%M")
    return f"{task_slug(task)}__{timestamp}"


def next_available_run_name(
    directory: Path,
    base_name: str,
    *,
    suffixes: tuple[str, ...],
) -> str:
    """Return a run name unused by every related output suffix.

    The first collision appends ``(1)`` before the suffix, followed by ``(2)``
    and so on. Checking all related suffixes keeps an evaluation result JSON and
    its artifacts directory on the same collision number.
    """

    if not suffixes:
        raise ValueError("at least one output suffix is required")
    number = 0
    while True:
        candidate = base_name if number == 0 else f"{base_name}({number})"
        if all(
            not (directory / f"{candidate}{suffix}").exists() for suffix in suffixes
        ):
            return candidate
        number += 1
