"""Append-only JSONL traces with reasoning redacted by default."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceWriter:
    def __init__(self, path: Path | None, *, include_reasoning: bool = False) -> None:
        self.path = path
        self.include_reasoning = include_reasoning
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "data": self._sanitize(data),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if key == "reasoning_content" and not self.include_reasoning:
                    text = item or ""
                    sanitized[key] = {
                        "redacted": True,
                        "chars": len(text),
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                else:
                    sanitized[key] = self._sanitize(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value

