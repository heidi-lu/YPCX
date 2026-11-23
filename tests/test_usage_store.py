"""usage_store 的离线测试：内存 SQLite，不碰真实库。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import usage_store
from models import Base


@pytest.fixture()
def memory_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(usage_store, "engine", engine)
    monkeypatch.setattr(usage_store, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False))
    monkeypatch.setattr(usage_store, "_tables_ready", True)
    return engine


def test_deepseek_peak_and_offpeak():
    at_peak = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)      # 周一忙时
    at_off = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)      # 周一错峰
    peak = usage_store.estimate_cost("deepseek", "deepseek-flash", 1_000_000, 1_000_000, at=at_peak)
    off = usage_store.estimate_cost("deepseek", "deepseek-flash", 1_000_000, 1_000_000, at=at_off)
    assert peak == pytest.approx(10.63)
    assert off == pytest.approx(peak / 2)


def test_deepseek_cache_discount():
    at_peak = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)
    cost = usage_store.estimate_cost(
        "deepseek", "deepseek-flash", 1_000_000, 0, cached_tokens=400_000, at=at_peak
    )
    assert cost == pytest.approx(1.2952)


def test_qwen_flat_and_tiered_prices():
    assert usage_store.estimate_cost("qwen", "qwen3.8-flash", 1_000_000, 1_000_000) == pytest.approx(3.5)
    assert usage_store.estimate_cost("qwen", "qwen3.7-flash", 20_000, 10_000) == pytest.approx(0.012)
    assert usage_store.estimate_cost("qwen", "qwen3.7-flash", 100_000, 10_000) == pytest.approx(0.084)


def test_model_snapshot_suffix_is_canonicalized():
    assert usage_store.estimate_cost("qwen", "qwen3.8-flash-2026-01-01", 1_000_000, 0) == pytest.approx(0.8)


def test_unknown_model_returns_none():
    assert usage_store.estimate_cost("deepseek", "no-such-model", 1000, 1000) is None


def test_record_and_summary(memory_db):
    usage_store.record_llm(
        stage="extract", provider="deepseek", model="deepseek-flash",
        prompt_tokens=1_000_000, completion_tokens=1_000_000, drug="测试药",
    )
    usage_store.record_search(query="测试药 获批", provider="deepseek", result_count=3, drug="测试药")
    summary = usage_store.cost_summary(days=7)
    assert summary["totals"]["llm_calls"] == 1
    assert summary["totals"]["searches"] == 1
    assert summary["totals"]["search_cny"] == pytest.approx(0.0053)
    assert summary["totals"]["llm_cny"] > 0
    assert summary["by_drug"][0]["drug"] == "测试药"
    assert usage_store.recent_usage(limit=5)["llm"][0]["stage"] == "extract"
