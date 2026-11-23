"""原始资料快照：把搜索命中、文档正文、LLM 请求按内容哈希落盘。

用途：
1. 复盘/审计：当时模型看到的是什么；
2. 离线回放：不重新付费搜索；
3. A/B：同一批输入喂给不同模型。

目录结构（默认 ./snapshots，已被 .gitignore 忽略）：
    snapshots/search/<sha1>.json.gz
    snapshots/llm/<sha1>.json.gz
    snapshots/doc/<sha1>.bin.gz
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from datetime import datetime

from config import SNAPSHOT_DIR, SNAPSHOT_ENABLED

logger = logging.getLogger(__name__)


def _digest(*parts: str) -> str:
    text = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _dir(kind: str) -> str:
    path = os.path.join(SNAPSHOT_DIR, kind)
    os.makedirs(path, exist_ok=True)
    return path


def _json_path(kind: str, key: str) -> str:
    return os.path.join(_dir(kind), f"{key}.json.gz")


def _bin_path(kind: str, key: str) -> str:
    return os.path.join(_dir(kind), f"{key}.bin.gz")


def _write_json(path: str, payload) -> None:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    with gzip.open(path, "wb") as handle:
        handle.write(raw)


def _read_json(path: str):
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def save_search(query: str, results: list, provider: str = "deepseek") -> str:
    """保存一次搜索结果；key 由 query+provider 决定，回放时可直接按 query 取。"""
    key = _digest("search", provider, query)
    if not SNAPSHOT_ENABLED:
        return key
    try:
        _write_json(_json_path("search", key), {
            "kind": "search",
            "query": query,
            "provider": provider,
            "results": results or [],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as exc:
        logger.warning("[快照] 搜索保存失败（忽略）: %s", exc)
    return key


def load_search(query: str, provider: str = "deepseek") -> list | None:
    key = _digest("search", provider, query)
    path = _json_path("search", key)
    if not os.path.exists(path):
        return None
    try:
        return _read_json(path).get("results") or []
    except Exception as exc:
        logger.warning("[快照] 搜索读取失败: %s", exc)
        return None


def save_llm(stage: str, model: str, messages: list, text: str) -> str:
    """保存一次 LLM 的完整输入与输出；key 由 stage+model+messages 决定。"""
    key = _digest("llm", stage, model, json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str))
    if not SNAPSHOT_ENABLED:
        return key
    try:
        _write_json(_json_path("llm", key), {
            "kind": "llm",
            "stage": stage,
            "model": model,
            "messages": messages,
            "response": text,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as exc:
        logger.warning("[快照] LLM 保存失败（忽略）: %s", exc)
    return key


def load_llm(key: str) -> dict | None:
    path = _json_path("llm", key)
    if not os.path.exists(path):
        return None
    try:
        return _read_json(path)
    except Exception as exc:
        logger.warning("[快照] LLM 读取失败: %s", exc)
        return None


def save_doc(url: str, content: bytes) -> str:
    """保存 PDF/PPT 原始字节，回放时不用重新下载。"""
    key = _digest("doc", url)
    if not SNAPSHOT_ENABLED or not content:
        return key
    try:
        if len(content) > 30 * 1024 * 1024:
            logger.info("[快照] 文档超过 30MB，跳过保存: %s", url)
            return key
        with gzip.open(_bin_path("doc", key), "wb") as handle:
            handle.write(content)
    except Exception as exc:
        logger.warning("[快照] 文档保存失败（忽略）: %s", exc)
    return key


def load_doc(url: str) -> bytes | None:
    path = _bin_path("doc", _digest("doc", url))
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rb") as handle:
            return handle.read()
    except Exception as exc:
        logger.warning("[快照] 文档读取失败: %s", exc)
        return None


def save_files(drug: str, files: list) -> str:
    key = _digest("files", drug)
    if not SNAPSHOT_ENABLED:
        return key
    try:
        _write_json(_json_path("files", key), {
            "kind": "files", "drug": drug, "files": files or [],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as exc:
        logger.warning("[快照] 文件列表保存失败（忽略）: %s", exc)
    return key


def load_files(drug: str) -> list | None:
    path = _json_path("files", _digest("files", drug))
    if not os.path.exists(path):
        return None
    try:
        return _read_json(path).get("files") or []
    except Exception:
        return None
