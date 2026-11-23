"""通用适应症获批日期解析：只在“证据句”里取日期，不猜、不按病名批量赋值。

规则（不依赖病名词典）：
1. 日期必须和病名出现在同一句；
2. 一线/二线/围术期/HER2/单药/鳞/非鳞等限定词必须一致；
3. “首次上市/首次获批”句里与药品上市同月的日期不算适应症获批；
4. 同一月份至少 2 个独立域名来源一致才采用，否则返回空。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

DATE_RE = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})(?:\s*[月\-/.]\s*(\d{1,2}))?")
SENTENCE_RE = re.compile(r"[。；;\n]")
LAUNCH_HINT = re.compile(r"首次上市|首次获批|首个适应症|第一项适应症")
LINE_WORDS = ("一线", "二线", "三线")


def _ym(value):
    m = re.match(r"(\d{4})\D+(\d{1,2})", str(value or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _features(full: str) -> dict:
    text = str(full or "")
    feats = {"line": "", "adjuvant": False, "her2": False, "mono": False, "subtype": ""}
    for word in LINE_WORDS:
        if word in text:
            feats["line"] = word
            break
    feats["adjuvant"] = bool(re.search(r"新辅助|围术期|辅助治疗", text))
    feats["her2"] = "维迪西妥" in text or "HER2" in text.upper()
    feats["mono"] = "单药" in text
    if "非鳞" in text:
        feats["subtype"] = "非鳞"
    elif "鳞状" in text or "鳞癌" in text:
        feats["subtype"] = "鳞"
    return feats


def _feature_ok(sentence: str, feats: dict) -> bool:
    if feats["line"] and feats["line"] not in sentence:
        return False
    if feats["adjuvant"] and not re.search(r"新辅助|围术期|辅助", sentence):
        return False
    if feats["her2"] and not ("维迪西妥" in sentence or "HER2" in sentence.upper()):
        return False
    if feats["mono"] and "单药" not in sentence:
        return False
    if feats["subtype"] == "非鳞" and "非鳞" not in sentence:
        return False
    if feats["subtype"] == "鳞" and "非鳞" in sentence:
        return False
    return True


def _valid_date(year: int, month: int, day: int, china_month) -> bool:
    now = datetime.now()
    if not (1990 <= year <= now.year and 1 <= month <= 12):
        return False
    if day and not 1 <= day <= 31:
        return False
    if (year, month) > (now.year, now.month):
        return False
    if china_month and (year, month) < china_month:
        return False
    return True


def dates_in_text(text: str, disease: str, full: str, china_date: str = "") -> list[tuple[str, str]]:
    """返回 (YYYY-MM-DD 或 YYYY-MM, 证据句) 列表。"""
    out = []
    china = _ym(china_date)
    feats = _features(full)
    for sentence in SENTENCE_RE.split(str(text or "")):
        sentence = sentence.strip()
        if not sentence or disease not in sentence or not _feature_ok(sentence, feats):
            continue
        if not re.search(r"批准|获批|上市|NMPA|FDA", sentence):
            continue
        launch_sentence = bool(LAUNCH_HINT.search(sentence))
        for m in DATE_RE.finditer(sentence):
            year, month = int(m.group(1)), int(m.group(2))
            day = int(m.group(3)) if m.group(3) else 0
            if not _valid_date(year, month, day, china):
                continue
            if launch_sentence and china and (year, month) == china:
                continue
            out.append((f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day else ""), sentence[:180]))
    return out


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def resolve_from_hits(hits: list[dict], disease: str, full: str, china_date: str = "") -> dict | None:
    """从搜索结果里找“同月、至少 2 个独立域名”的日期。"""
    buckets = defaultdict(lambda: {"domains": set(), "url": "", "evidence": "", "date": ""})
    for hit in hits or []:
        text = f"{hit.get('title', '')} {hit.get('content', '')}"
        url = str(hit.get("url") or "")
        domain = _domain(url)
        for date, evidence in dates_in_text(text, disease, full, china_date):
            bucket = buckets[date[:7]]
            bucket["domains"].add(domain)
            if len(date) > len(bucket["date"]):
                bucket["date"] = date
            if not bucket["url"]:
                bucket["url"], bucket["evidence"] = url, evidence
    ranked = sorted(
        ((month, b) for month, b in buckets.items() if len(b["domains"]) >= 2),
        key=lambda kv: -len(kv[1]["domains"]),
    )
    if not ranked:
        return None
    best_month, best = ranked[0]
    if len(ranked) > 1 and len(ranked[1][1]["domains"]) == len(best["domains"]):
        return None  # 两个月份打平，无法判断
    return {"date": best.get("date") or best_month, "source": best["url"], "evidence": best["evidence"]}


def resolve_indication_approval_date(
    search_fn, generic: str, full: str, short: str, china_date: str = "", max_results: int = 6
) -> dict | None:
    """通用入口：网搜 + 证据句 + 双来源校验。"""
    disease = re.sub(r"[（(].*?[）)]", "", str(short or "")).strip()
    if len(disease) < 2 or not str(full or "").strip():
        return None
    query = f"{generic} {disease} 获批 时间 NMPA"
    hits = search_fn(query, max_results)
    return resolve_from_hits(hits, disease, full, china_date)
