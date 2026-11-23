import time
import threading
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import source_snapshot
import usage_store
from config import SEARCH_API_KEY, SEARCH_BASE_URL, HTTP_RETRY

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_last_request_time = 0
_MIN_INTERVAL = 0.2
_session = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                retry = Retry(
                    total=HTTP_RETRY, backoff_factor=0.5,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=("POST",),
                )
                adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _session = s
    return _session


def _rate_limit():
    global _last_request_time
    with _lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
            now = time.time()
        _last_request_time = now


def _finish(query: str, results: list, started: float, provider: str, status: str = "ok") -> list:
    """统一记录用量 + 快照，返回结果本身。"""
    latency_ms = int((time.monotonic() - started) * 1000)
    snapshot_key = ""
    if results:
        snapshot_key = source_snapshot.save_search(query, results, provider)
    usage_store.record_search(
        query=query,
        provider=provider,
        result_count=len(results),
        latency_ms=latency_ms,
        status=status if results else (status if status != "ok" else "empty"),
        snapshot_key=snapshot_key,
    )
    return results


def search(query: str, max_results: int = 5, retries: int = 2, provider: str = "deepseek"):
    started = time.monotonic()
    if not SEARCH_API_KEY:
        logger.error("[搜索] SEARCH_API_KEY 未配置，所有网页搜索将返回空！")
        return _finish(query, [], started, provider, status="error")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SEARCH_API_KEY}",
    }
    payload = {"query": query, "max_results": max_results}
    session = _get_session()

    for attempt in range(retries + 1):
        try:
            _rate_limit()
            resp = session.post(SEARCH_BASE_URL, headers=headers, json=payload, timeout=15)
            if resp.status_code == 401:
                logger.error("[搜索] 401 鉴权失败，请检查 SEARCH_API_KEY")
                return _finish(query, [], started, provider, status="error")
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                return _finish(query, [], started, provider, status="error")

            raw_results = data.get("results", []) or data.get("data", {}).get("results", [])
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url") or r.get("link", ""),
                    "content": r.get("content") or r.get("snippet") or r.get("summary", ""),
                }
                for r in raw_results[:max_results]
            ]
            return _finish(query, results, started, provider)
        except Exception as e:
            if attempt == retries:
                logger.warning(f"[搜索] 失败 ({query}): {e}")
                return _finish(query, [], started, provider, status="error")
            time.sleep(1)
    return _finish(query, [], started, provider, status="error")
