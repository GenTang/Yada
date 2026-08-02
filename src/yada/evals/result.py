"""Serialization helpers for durable evaluation results."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from yada.evals.base import EvalResult


def result_to_dict(result: EvalResult) -> dict[str, Any]:
    """Convert an evaluation result to a JSON-safe mapping."""

    data = asdict(result)
    data["status"] = result.status
    return data


def write_result(path: Path, result: EvalResult) -> None:
    """Atomically write one pretty-printed evaluation result."""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(result_to_dict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
