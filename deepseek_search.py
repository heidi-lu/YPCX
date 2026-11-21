import requests
import time
import threading
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

def search(query: str, max_results: int = 5, retries: int = 2):
    if not SEARCH_API_KEY:
        logger.error("[搜索] SEARCH_API_KEY 未配置，所有网页搜索将返回空！")
        return []
    
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
                return []
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            try: data = resp.json()
            except ValueError: return []
            
            raw_results = data.get("results", []) or data.get("data", {}).get("results", [])
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url") or r.get("link", ""),
                    "content": r.get("content") or r.get("snippet") or r.get("summary", ""),
                }
                for r in raw_results[:max_results]
            ]
        except Exception as e:
            if attempt == retries:
                logger.warning(f"[搜索] 失败 ({query}): {e}")
                return []
            time.sleep(1)
    return []