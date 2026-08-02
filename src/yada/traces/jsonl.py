"""Append-only JSONL traces with stable correlation metadata.

JSONL remains the durable source of truth because it is streamable and survives a
crashed process. Each event also carries a run ID, sequence number, schema version,
and elapsed time so diagnostic views can reconstruct causality without guessing.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = 1


class TraceWriter:
    """Write sanitized, self-correlating events to an append-only JSONL file.

    Args:
        path: Destination file, or ``None`` to disable tracing.
        include_reasoning: Persist raw ``reasoning_content`` when true. The
            default stores only its length and SHA-256 digest.
        run_id: Optional stable identifier, primarily useful for deterministic
            tests or importing events from an external orchestrator.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        include_reasoning: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.path = path
        self.include_reasoning = include_reasoning
        self.run_id = run_id or uuid.uuid4().hex
        self._sequence = 0
        self._started = time.monotonic()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, data: dict[str, Any]) -> None:
        """Append one event after recursively sanitizing sensitive reasoning.

        Args:
            event: Stable event type such as ``model_request`` or ``tool_result``.
            data: JSON-like event payload. Unknown values fall back to ``str``
                during serialization so observability cannot break agent execution.
        """

        if self.path is None:
            return
        if not event or not isinstance(event, str):
            raise ValueError("trace event must be a non-empty string")
        self._sequence += 1
        record = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "sequence": self._sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.monotonic() - self._started) * 1000),
            "event": event,
            "data": self._sanitize(data),
        }
        # Opening per event is intentional: every completed write is immediately
        # visible to tailing/debugging tools and survives a later process crash.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if key == "reasoning_content" and not self.include_reasoning:
                    text = str(item or "")
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
        if isinstance(value, tuple):
            return [self._sanitize(item) for item in value]
        return value
