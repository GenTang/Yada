from __future__ import annotations

import json

from yada.models import DeepSeekClient


def test_endpoint_normalization() -> None:
    assert (
        DeepSeekClient._completion_endpoint("https://api.deepseek.com/")
        == "https://api.deepseek.com/chat/completions"
    )
    assert (
        DeepSeekClient._completion_endpoint("https://example.test/chat/completions")
        == "https://example.test/chat/completions"
    )


def test_completion_uses_deepseek_thinking_contract(monkeypatch) -> None:
    client = DeepSeekClient(api_key="test-key", reasoning_effort="max")
    response = {
        "id": "response-1",
        "model": "deepseek-v4-pro",
        "system_fingerprint": "fingerprint-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "must be passed back",
                    "tool_calls": [],
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }
    sent = []

    def fake_send(request):
        sent.append(request)
        return response

    monkeypatch.setattr(client, "_send_with_retries", fake_send)
    completion = client.complete(
        messages=[{"role": "user", "content": "hello"}], tools=[]
    )

    payload = json.loads(sent[0].data.decode("utf-8"))
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert completion.message["reasoning_content"] == "must be passed back"
    assert completion.system_fingerprint == "fingerprint-1"

