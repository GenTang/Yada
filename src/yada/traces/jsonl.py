"""Append-only JSONL traces with stable correlation metadata.

JSONL remains the durable source of truth because it is streamable and survives a
crashed process. Each event also carries a run ID, sequence number, schema version,
and elapsed time so diagnostic views can reconstruct causality without guessing.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = 3
TRACE_LEVELS = {"summary", "debug"}
_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "authorization",
    "credential",
    "credentials",
    "password",
    "proxy_authorization",
    "secret",
    "token",
}
_SECRET_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_auth_token",
    "_credential",
    "_credentials",
    "_password",
    "_refresh_token",
    "_secret",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"refresh[_-]?token|password|secret|credential)s?\s*[:=]\s*)"
        r"(?:[\"'][^\"']*[\"']|[^\s,;]+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


class TraceWriter:
    """Write sanitized, self-correlating events to an append-only JSONL file.

    Args:
        path: Destination file, or ``None`` to disable tracing.
        level: ``summary`` stores compact request metrics and redacts reasoning;
            ``debug`` stores sanitized provider payloads and reasoning text.
        run_id: Optional stable identifier, primarily useful for deterministic
            tests or importing events from an external orchestrator.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        level: str = "summary",
        run_id: str | None = None,
    ) -> None:
        if level not in TRACE_LEVELS:
            raise ValueError(f"trace level must be one of {sorted(TRACE_LEVELS)}")
        self.path = path
        self.level = level
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
                if key == "reasoning_content" and self.level != "debug":
                    text = str(item or "")
                    sanitized[key] = {
                        "redacted": True,
                        "chars": len(text),
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                elif _is_secret_key(key):
                    sanitized[key] = _REDACTED
                else:
                    sanitized[key] = self._sanitize(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return _sanitize_text(value)
        return value


def _is_secret_key(key: Any) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


def _sanitize_text(value: str) -> str:
    sanitized = _SECRET_TEXT_PATTERNS[0].sub(r"\1[REDACTED]", value)
    sanitized = _SECRET_TEXT_PATTERNS[1].sub(r"\1[REDACTED]", sanitized)
    return _SECRET_TEXT_PATTERNS[2].sub(_REDACTED, sanitized)
