"""Readable, portable names for local run artifacts."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

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
    """Combine a task slug with a sortable, readable UTC timestamp."""

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    timestamp = instant.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S.%fZ")
    return f"{task_slug(task)}__{timestamp}"
