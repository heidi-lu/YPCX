"""LLM / 搜索的用量与成本记录（P0）。

原则：
1. 记录失败绝不影响主流程（吞异常 + 警告日志）；
2. 每次尝试都记一条，重试产生的费用不会漏；
3. 成本按 config.LLM_PRICES 计算，未知模型 cost=None 并只告警一次。
"""
from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from config import LLM_PRICES, SEARCH_PRICES, USAGE_LOG_ENABLED
from db import SessionLocal, engine
from models import Base, LlmUsage, SearchUsage

logger = logging.getLogger(__name__)

_drug_ctx: ContextVar[str] = ContextVar("usage_drug", default="")
_tables_ready = False
_ensure_lock = threading.Lock()
_warned_prices: set[tuple[str, str]] = set()
_warn_lock = threading.Lock()


def set_drug(name: str):
    """在当前执行上下文里标记药品名；搜索/LLM 记录会自动带上。"""
    return _drug_ctx.set(str(name or ""))


def reset_drug(token) -> None:
    try:
        _drug_ctx.reset(token)
    except (ValueError, LookupError):  # 跨线程 reset 失败时忽略即可
        pass


def current_drug() -> str:
    return _drug_ctx.get()


def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with _ensure_lock:
        if _tables_ready:
            return
        Base.metadata.create_all(engine)
        _tables_ready = True


def _canonical_model(provider: str, model: str) -> str:
    """把带日期后缀的模型名归一到价目表里的键。"""
    model = str(model or "")
    if (provider, model) in LLM_PRICES:
        return model
    candidates = [key for (prov, key) in LLM_PRICES if prov == provider and model.startswith(key)]
    if candidates:
        return max(candidates, key=len)
    return model


def _price_entry(provider: str, model: str, prompt_tokens: int) -> dict | None:
    entry = LLM_PRICES.get((provider, model))
    if not entry:
        return None
    if "tiers" not in entry:
        return entry
    for tier in sorted(entry["tiers"], key=lambda t: t["max_in"]):
        if prompt_tokens <= tier["max_in"]:
            return tier
    return entry["tiers"][-1]


def _is_deepseek_peak(at: datetime) -> bool:
    """DeepSeek 忙时：UTC 周一至周五 01:00-04:00、06:00-10:00；其余（含周末）错峰。"""
    utc = at.astimezone(timezone.utc)
    if utc.weekday() >= 5:
        return False
    return 1 <= utc.hour < 4 or 6 <= utc.hour < 10


def estimate_cost(
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    at: datetime | None = None,
) -> float | None:
    """按官方价目表估算单次调用成本（元）。未知模型返回 None。"""
    provider = str(provider or "")
    canonical = _canonical_model(provider, model)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cached_tokens = min(max(0, int(cached_tokens or 0)), prompt_tokens)

    entry = _price_entry(provider, canonical, prompt_tokens)
    if not entry:
        with _warn_lock:
            if (provider, canonical) not in _warned_prices:
                _warned_prices.add((provider, canonical))
                logger.warning("[成本] 未知价目，cost 记为空: %s / %s", provider, canonical)
        return None

    uncached = prompt_tokens - cached_tokens
    cost = (
        uncached * float(entry["in"])
        + cached_tokens * float(entry.get("cache", entry["in"]))
        + completion_tokens * float(entry["out"])
    ) / 1_000_000
    if entry.get("offpeak_half") and not _is_deepseek_peak(at or datetime.now(timezone.utc)):
        cost *= 0.5
    return round(cost, 6)


def record_llm(
    *,
    stage: str,
    provider: str,
    model: str,
    thinking: bool = False,
    attempt: int = 1,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    error: str = "",
    snapshot_key: str = "",
    drug: str = "",
) -> None:
    if not USAGE_LOG_ENABLED:
        return
    try:
        cost = estimate_cost(provider, model, prompt_tokens, completion_tokens, cached_tokens)
        _ensure_tables()
        with SessionLocal() as session:
            session.add(LlmUsage(
                drug=drug or current_drug(),
                stage=stage,
                provider=provider,
                model=model,
                thinking=bool(thinking),
                attempt=int(attempt),
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                reasoning_tokens=int(reasoning_tokens or 0),
                cached_tokens=int(cached_tokens or 0),
                latency_ms=int(latency_ms or 0),
                cost_cny=cost,
                status=status,
                error=str(error or "")[:2000],
                snapshot_key=snapshot_key,
            ))
            session.commit()
    except Exception as exc:  # 记录失败不能影响查询
        logger.warning("[用量] LLM 记录失败（忽略）: %s", exc)


def record_search(
    *,
    query: str,
    provider: str = "deepseek",
    result_count: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    snapshot_key: str = "",
    drug: str = "",
) -> None:
    if not USAGE_LOG_ENABLED:
        return
    try:
        unit = float(SEARCH_PRICES.get(provider, 0.0))
        cost = unit if status == "ok" and result_count > 0 else 0.0
        _ensure_tables()
        with SessionLocal() as session:
            session.add(SearchUsage(
                drug=drug or current_drug(),
                query=str(query or "")[:8000],
                provider=provider,
                result_count=int(result_count or 0),
                latency_ms=int(latency_ms or 0),
                cost_cny=round(cost, 6),
                status=status,
                snapshot_key=snapshot_key,
            ))
            session.commit()
    except Exception as exc:
        logger.warning("[用量] 搜索记录失败（忽略）: %s", exc)


def _aggregate(rows: list, key_fields: tuple[str, ...]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(getattr(row, field, "") for field in key_fields)
        bucket = buckets.setdefault(key, {
            **{field: getattr(row, field, "") for field in key_fields},
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_cny": 0.0,
        })
        bucket["calls"] += 1
        bucket["prompt_tokens"] += int(row.prompt_tokens or 0)
        bucket["completion_tokens"] += int(row.completion_tokens or 0)
        bucket["cost_cny"] += float(row.cost_cny or 0.0)
    for bucket in buckets.values():
        bucket["cost_cny"] = round(bucket["cost_cny"], 4)
    return sorted(buckets.values(), key=lambda item: item["cost_cny"], reverse=True)


def cost_summary(days: int = 7) -> dict:
    """按模型 / 阶段 / 药品聚合近 N 天成本。"""
    days = max(1, int(days or 7))
    since = datetime.now() - timedelta(days=days)
    _ensure_tables()
    with SessionLocal() as session:
        llm_rows = session.query(LlmUsage).filter(LlmUsage.created_at >= since).all()
        search_rows = session.query(SearchUsage).filter(SearchUsage.created_at >= since).all()
        total_llm = session.query(func.coalesce(func.sum(LlmUsage.cost_cny), 0.0)).filter(
            LlmUsage.created_at >= since
        ).scalar()
        total_search = session.query(func.coalesce(func.sum(SearchUsage.cost_cny), 0.0)).filter(
            SearchUsage.created_at >= since
        ).scalar()

    return {
        "since": since.isoformat(timespec="seconds"),
        "totals": {
            "llm_cny": round(float(total_llm or 0.0), 4),
            "search_cny": round(float(total_search or 0.0), 4),
            "cny": round(float(total_llm or 0.0) + float(total_search or 0.0), 4),
            "llm_calls": len(llm_rows),
            "searches": len(search_rows),
        },
        "by_model": _aggregate(llm_rows, ("provider", "model")),
        "by_stage": _aggregate(llm_rows, ("stage",)),
        "by_drug": _aggregate(llm_rows, ("drug",)),
    }


def recent_usage(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 500))
    _ensure_tables()
    with SessionLocal() as session:
        llm_rows = session.query(LlmUsage).order_by(LlmUsage.id.desc()).limit(limit).all()
        search_rows = session.query(SearchUsage).order_by(SearchUsage.id.desc()).limit(limit).all()

    def _row(obj, fields):
        return {field: getattr(obj, field, "") for field in fields}

    return {
        "llm": [
            _row(row, (
                "id", "created_at", "drug", "stage", "provider", "model", "thinking", "attempt",
                "prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
                "latency_ms", "cost_cny", "status", "error", "snapshot_key",
            ))
            for row in llm_rows
        ],
        "search": [
            _row(row, (
                "id", "created_at", "drug", "query", "provider", "result_count",
                "latency_ms", "cost_cny", "status", "snapshot_key",
            ))
            for row in search_rows
        ],
    }
