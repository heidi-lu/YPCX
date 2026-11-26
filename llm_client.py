"""统一 LLM 调用入口（P0）。

- 兼容 DeepSeek / 千问（OpenAI 协议），按模型名自动选 endpoint；
- thinking 开关：DeepSeek 用 extra_body.thinking，千问用 enable_thinking；
- 每次尝试都记 usage（含失败），成本按 config.LLM_PRICES 计算；
- 调用方拿到 LLMResult 或 None；需要区分错误类型时传 raise_on_error=True。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from openai import OpenAI

import source_snapshot
import usage_store
from config import (
    LLM_MAX_CONCURRENCY,
    MAIN_LLM_API_KEY,
    MAIN_LLM_BASE_URL,
    MAIN_LLM_MODEL,
    MODEL_ROUTES,
    QWEN_LLM_API_KEY,
    QWEN_LLM_BASE_URL,
    THINK_EFFORT,
    THINK_ENABLED,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_STAGES = {"web", "think"}
_clients: dict[tuple[str, str], OpenAI] = {}
_client_lock = threading.Lock()
_semaphore = threading.Semaphore(max(1, LLM_MAX_CONCURRENCY))


class LLMError(RuntimeError):
    """不可重试的调用错误（鉴权/欠费/参数错误）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    cost_cny: float | None = None
    attempt: int = 1
    snapshot_key: str = ""


def _is_qwen_model(model: str) -> bool:
    return str(model or "").lower().startswith(("qwen", "qvq"))


def _endpoint(model: str) -> tuple[str, str, str]:
    """返回 (provider, base_url, api_key)。"""
    if _is_qwen_model(model):
        return "qwen", QWEN_LLM_BASE_URL, QWEN_LLM_API_KEY
    provider = "qwen" if "dashscope" in str(MAIN_LLM_BASE_URL).lower() else "deepseek"
    return provider, MAIN_LLM_BASE_URL, MAIN_LLM_API_KEY


def _client(base_url: str, api_key: str) -> OpenAI:
    key = (base_url, api_key)
    with _client_lock:
        if key not in _clients:
            _clients[key] = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        return _clients[key]


def _usage_dict(response) -> dict:
    usage = getattr(response, "usage", None)
    data = {
        "prompt_tokens": 0, "completion_tokens": 0,
        "reasoning_tokens": 0, "cached_tokens": 0,
    }
    if not usage:
        return data
    data["prompt_tokens"] = int(getattr(usage, "prompt_tokens", 0) or 0)
    data["completion_tokens"] = int(getattr(usage, "completion_tokens", 0) or 0)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details is not None:
        data["cached_tokens"] = int(getattr(prompt_details, "cached_tokens", 0) or 0)
    if not data["cached_tokens"]:
        data["cached_tokens"] = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    completion_details = getattr(usage, "completion_tokens_details", None)
    if completion_details is not None:
        data["reasoning_tokens"] = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
    return data


def _thinking_kwargs(provider: str, model: str, thinking: bool) -> dict:
    if provider == "qwen":
        return {"extra_body": {"enable_thinking": bool(thinking)}}
    if thinking:
        return {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": THINK_EFFORT,
        }
    # 老模型（deepseek-chat）不认识 thinking 参数，保持原样不发，避免 400。
    if str(model).startswith(("deepseek-flash", "deepseek-v4")):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def chat(
    messages: list,
    model: str | None = None,
    stage: str = "extract",
    thinking: bool | None = None,
    json_mode: bool = True,
    timeout: int = 40,
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_attempts: int = 2,
    raise_on_error: bool = False,
    drug: str = "",
) -> LLMResult | None:
    """调用文本模型；失败返回 None（或按 raise_on_error 抛 LLMError）。"""
    resolved_model = model or MODEL_ROUTES.get(stage, MAIN_LLM_MODEL)
    provider, base_url, api_key = _endpoint(resolved_model)
    if not api_key:
        logger.error("[LLM] %s 未配置 API Key（stage=%s）", provider, stage)
        if raise_on_error:
            raise LLMError(f"{provider} API Key 未配置")
        return None
    use_thinking = THINK_ENABLED if thinking is None else bool(thinking)
    client = _client(base_url, api_key)

    last_error: Exception | None = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        started = time.monotonic()
        try:
            kwargs = {
                "model": resolved_model,
                "messages": messages,
                "timeout": timeout,
                **_thinking_kwargs(provider, resolved_model, use_thinking),
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if temperature is not None and not use_thinking:
                kwargs["temperature"] = temperature
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            with _semaphore:
                response = client.chat.completions.create(**kwargs)
            text = (response.choices[0].message.content or "").strip()
            usage = _usage_dict(response)
            latency_ms = int((time.monotonic() - started) * 1000)
            cost = usage_store.estimate_cost(
                provider, resolved_model,
                usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"],
            )
            snapshot_key = ""
            if stage in _SNAPSHOT_STAGES:
                snapshot_key = source_snapshot.save_llm(stage, resolved_model, messages, text)
            usage_store.record_llm(
                stage=stage, provider=provider, model=resolved_model, thinking=use_thinking,
                attempt=attempt, latency_ms=latency_ms, status="ok",
                snapshot_key=snapshot_key, drug=drug,
                **usage,
            )
            return LLMResult(
                text=text, provider=provider, model=resolved_model, usage=usage,
                latency_ms=latency_ms, cost_cny=cost, attempt=attempt, snapshot_key=snapshot_key,
            )
        except Exception as exc:  # noqa: BLE001 - 需要按状态码区分
            last_error = exc
            status = getattr(exc, "status_code", None)
            latency_ms = int((time.monotonic() - started) * 1000)
            usage_store.record_llm(
                stage=stage, provider=provider, model=resolved_model, thinking=use_thinking,
                attempt=attempt, latency_ms=latency_ms, status="error", error=str(exc),
                drug=drug,
            )
            non_retryable = status in {400, 401, 402, 403, 404}
            logger.warning(
                "[LLM] 失败 provider=%s model=%s stage=%s attempt=%s status=%s: %s",
                provider, resolved_model, stage, attempt, status, exc,
            )
            if non_retryable:
                if raise_on_error:
                    raise LLMError(str(exc), status_code=status) from exc
                return None
            if attempt < max_attempts:
                time.sleep(min(2 * attempt, 4))

    if raise_on_error and last_error is not None:
        raise LLMError(str(last_error))
    return None
