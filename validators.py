# validators.py
import re
from datetime import datetime

def validate_date(date_str: str) -> str:
    """时间防穿越锁：> 当前年份 或 < 1990 直接丢弃。保留原格式（YYYY / YYYY-MM / YYYY-MM-DD）。"""
    if not date_str:
        return ""
    m = re.search(r'(\d{4})', str(date_str))
    if not m:
        return ""
    year = int(m.group(1))
    current_year = datetime.now().year
    if year > current_year or year < 1990:
        return ""
    return str(date_str).strip()

def normalize_date_ym(date_str: str) -> str:
    """VLM 输出的日期统一为 YYYY-MM，非法返回空。"""
    if not date_str:
        return ""
    m = re.search(r'(\d{4})\D{0,3}(\d{1,2})', str(date_str))
    if not m:
        return ""
    y, mo = int(m.group(1)), int(m.group(2))
    current_year = datetime.now().year
    if y > current_year or y < 1990:
        return ""
    if not (1 <= mo <= 12):
        return f"{y:04d}"
    return f"{y:04d}-{mo:02d}"