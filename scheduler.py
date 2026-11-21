import json
import time
import hashlib
import threading
from typing import Callable, Dict
import logging

logger = logging.getLogger(__name__)

# 单位：天。NHSA 每年更新，30 天足够
FIELD_TTL = {
    "nhsa_pdf": 30,
}


class _MemoryCache:
    def __init__(self):
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            value, expire_at = item
            if expire_at is not None and time.time() > expire_at:
                del self._store[key]
                return None
            return value

    def setex(self, key, ttl_seconds, value):
        with self._lock:
            if ttl_seconds is None:
                expire_at = None
            elif ttl_seconds <= 0:
                expire_at = time.time()  # 立即过期（语义正确，不用 falsy 判断）
            else:
                expire_at = time.time() + ttl_seconds
            self._store[key] = (value, expire_at)


class DrugDataScheduler:
    def __init__(self, redis_client=None):
        self.redis = None
        self.memory = _MemoryCache()
        self._flight_locks: Dict[str, threading.Lock] = {}
        self._flight_lock = threading.Lock()

        if redis_client:
            try:
                redis_client.ping()
                self.redis = redis_client
                logger.info("[缓存] 使用 Redis")
            except Exception:
                logger.warning("[缓存] Redis 不可用，使用内存缓存")
        else:
            logger.info("[缓存] 使用内存缓存")

    def _make_key(self, field_group: str, drug_name: str, extra_keys: dict = None) -> str:
        # 用 \x1f 作为分隔符，避免药品名里含 ":" 造成歧义
        base = f"drug\x1f{drug_name}\x1f{field_group}"
        if extra_keys:
            sorted_extra = sorted(extra_keys.items())
            extra_str = "|".join(f"{k}={v}" for k, v in sorted_extra)
            h = hashlib.md5(extra_str.encode()).hexdigest()[:8]
            base += f"\x1f{h}"
        return base

    def _flight_lock_for(self, key: str) -> threading.Lock:
        with self._flight_lock:
            if key not in self._flight_locks:
                self._flight_locks[key] = threading.Lock()
            return self._flight_locks[key]

    def _get(self, key):
        if self.redis:
            try:
                return self.redis.get(key)
            except Exception:
                pass
        return self.memory.get(key)

    def _set(self, key, ttl_seconds, value):
        if self.redis:
            try:
                if ttl_seconds is not None and ttl_seconds > 0:
                    self.redis.setex(key, ttl_seconds, value)
                else:
                    self.redis.set(key, value)
                return
            except Exception:
                pass
        self.memory.setex(key, ttl_seconds, value)

    def get_or_fetch(self, field_group: str, drug_name: str, fetch_func: Callable, extra_keys: dict = None):
        key = self._make_key(field_group, drug_name, extra_keys)
        ttl_days = FIELD_TTL.get(field_group, 90)
        ttl_seconds = ttl_days * 86400

        # 1) 先查缓存
        try:
            cached = self._get(key)
            if cached:
                logger.info(f"[缓存命中] {field_group} - {drug_name}")
                return json.loads(cached) if isinstance(cached, str) else cached
        except Exception:
            pass

        # 2) singleflight：同一 key 并发时只让一个线程去取
        flight_lock = self._flight_lock_for(key)
        with flight_lock:
            try:
                cached = self._get(key)
                if cached:
                    return json.loads(cached) if isinstance(cached, str) else cached
            except Exception:
                pass

            try:
                data = fetch_func(drug_name, field_group, extra_keys)
            except Exception as e:
                logger.warning(f"[获取失败] {field_group} - {drug_name}: {e}")
                return None

            if data:
                try:
                    self._set(key, ttl_seconds, json.dumps(data, ensure_ascii=False))
                except Exception as e:
                    logger.warning(f"[缓存写入失败] {e}")
            return data