"""A tiny DeepSeek Chat Completions client using only the Python standard library."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from yada.models.base import Completion


class DeepSeekAPIError(RuntimeError):
    """Raised when a DeepSeek request cannot be completed."""


class DeepSeekClient:
    """Minimal synchronous client for DeepSeek's OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-pro",
        thinking: bool = True,
        reasoning_effort: str = "max",
        max_output_tokens: int = 16_384,
        timeout_seconds: int = 300,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("A DeepSeek API key is required")
        if reasoning_effort not in {"high", "max"}:
            raise ValueError("reasoning_effort must be 'high' or 'max'")
        self.api_key = api_key
        self.endpoint = self._completion_endpoint(base_url)
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    @staticmethod
    def _completion_endpoint(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        return f"{normalized}/chat/completions"

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        """Send one OpenAI-compatible chat-completions request.

        Args:
            messages: Append-only conversation history, including tool results.
            tools: Function schemas available to the model on this turn.

        Returns:
            Normalized assistant message, provider metadata, and usage counters.

        Raises:
            DeepSeekAPIError: If transport retries fail or the response is malformed.
        """

        payload = self.request_payload(messages=messages, tools=tools)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "yada-agent/0.1.0",
            },
        )
        response_data = self._send_with_retries(request)
        try:
            choice = response_data["choices"][0]
            raw_message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekAPIError(
                f"Malformed DeepSeek response: {json.dumps(response_data)[:1000]}"
            ) from exc

        # DeepSeek requires reasoning_content to be sent back after tool calls.
        message = {
            key: raw_message[key]
            for key in ("role", "content", "reasoning_content", "tool_calls")
            if key in raw_message
        }
        message_field_presence = {
            key: key in raw_message
            for key in ("role", "content", "reasoning_content", "tool_calls")
        }
        message.setdefault("role", "assistant")
        message.setdefault("content", "")
        return Completion(
            message=message,
            usage=response_data.get("usage") or {},
            response_id=response_data.get("id"),
            model=response_data.get("model"),
            system_fingerprint=response_data.get("system_fingerprint"),
            finish_reason=choice.get("finish_reason"),
            message_field_presence=message_field_presence,
        )

    def request_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the exact JSON body used by ``complete`` for tracing parity."""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "max_tokens": self.max_output_tokens,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if self.thinking:
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            # DeepSeek V4 thinking mode rejects tool_choice. Non-thinking mode
            # accepts the ordinary OpenAI-compatible automatic selection value.
            payload["tool_choice"] = "auto"
        return payload

    def trace_config(self) -> dict[str, Any]:
        """Return model settings safe to persist in ``run_start``."""

        return {
            "provider": "deepseek",
            "model": self.model,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort if self.thinking else None,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }

    def _send_with_retries(self, request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    if not isinstance(data, dict):
                        raise DeepSeekAPIError(
                            "DeepSeek returned a non-object JSON body"
                        )
                    return data
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")[:2000]
                retryable = exc.code in {408, 409, 429} or 500 <= exc.code < 600
                last_error = DeepSeekAPIError(
                    f"DeepSeek HTTP {exc.code}: {error_body or exc.reason}"
                )
                if not retryable or attempt >= self.max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
            time.sleep(min(2**attempt, 8))
        raise DeepSeekAPIError(f"DeepSeek request failed: {last_error}") from last_error
