"""快照读写测试：使用临时目录，不写项目 snapshots/。"""
from __future__ import annotations

import source_snapshot


def test_search_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ENABLED", True)
    results = [{"title": "t", "url": "https://example.com", "content": "c"}]
    key = source_snapshot.save_search("测试 查询", results)
    assert key
    assert source_snapshot.load_search("测试 查询") == results


def test_llm_roundtrip_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ENABLED", True)
    messages = [{"role": "user", "content": "hi"}]
    key = source_snapshot.save_llm("think", "qwen3.8-flash", messages, "answer")
    saved = source_snapshot.load_llm(key)
    assert saved["response"] == "answer"
    assert saved["messages"] == messages
    assert source_snapshot.load_llm("not-exists") is None


def test_doc_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ENABLED", True)
    payload = b"%PDF-1.4 fake"
    source_snapshot.save_doc("https://example.com/a.pdf", payload)
    assert source_snapshot.load_doc("https://example.com/a.pdf") == payload
    assert source_snapshot.load_doc("https://example.com/missing.pdf") is None


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ENABLED", False)
    source_snapshot.save_search("q", [{"title": "x"}])
    assert source_snapshot.load_search("q") is None
    assert not any(tmp_path.rglob("*.gz"))
