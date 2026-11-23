"""llm_client 的离线测试：不联网、不写库。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import llm_client


class _FakeCompletions:
    def __init__(self, error=None):
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=200,
                prompt_tokens_details=SimpleNamespace(cached_tokens=400),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=50),
            ),
        )


@pytest.fixture()
def fake(monkeypatch):
    completions = _FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm_client, "_client", lambda base_url, api_key: client)
    monkeypatch.setattr(llm_client, "MAIN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "QWEN_LLM_API_KEY", "test-key")
    records = []
    monkeypatch.setattr(llm_client.usage_store, "record_llm", lambda **kwargs: records.append(kwargs))
    monkeypatch.setattr(llm_client.source_snapshot, "save_llm", lambda *a, **k: "snap")
    return SimpleNamespace(completions=completions, records=records)


def test_chat_returns_text_and_usage(fake):
    result = llm_client.chat(
        [{"role": "user", "content": "hi"}], model="deepseek-flash", stage="extract",
        thinking=False, json_mode=True,
    )
    assert result is not None
    assert result.text == '{"ok": true}'
    assert result.usage["prompt_tokens"] == 1000
    assert result.usage["cached_tokens"] == 400
    assert result.usage["reasoning_tokens"] == 50
    assert fake.records[-1]["status"] == "ok"
    assert fake.records[-1]["prompt_tokens"] == 1000


def test_deepseek_thinking_flags(fake):
    llm_client.chat([{"role": "user", "content": "hi"}], model="deepseek-flash", thinking=True)
    kwargs = fake.completions.kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["reasoning_effort"]


def test_qwen_thinking_flags(fake):
    llm_client.chat([{"role": "user", "content": "hi"}], model="qwen3.8-flash", thinking=True)
    kwargs = fake.completions.kwargs
    assert kwargs["extra_body"] == {"enable_thinking": True}


def test_json_mode_can_be_disabled(fake):
    llm_client.chat([{"role": "user", "content": "hi"}], model="deepseek-flash", json_mode=False)
    assert "response_format" not in fake.completions.kwargs


def test_non_retryable_error_returns_none(fake, monkeypatch):
    error = RuntimeError("bad key")
    error.status_code = 401
    monkeypatch.setattr(llm_client, "_client", lambda base_url, api_key: SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(error=error))
    ))
    result = llm_client.chat([{"role": "user", "content": "hi"}], model="deepseek-flash", max_attempts=1)
    assert result is None
    assert fake.records[-1]["status"] == "error"


def test_non_retryable_error_can_raise(fake, monkeypatch):
    error = RuntimeError("arrearage")
    error.status_code = 402
    monkeypatch.setattr(llm_client, "_client", lambda base_url, api_key: SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(error=error))
    ))
    with pytest.raises(llm_client.LLMError):
        llm_client.chat([{"role": "user", "content": "hi"}], model="deepseek-flash",
                        max_attempts=1, raise_on_error=True)
