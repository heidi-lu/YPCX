"""病名同义/变体表：同一份 medical_aliases.json 只解析一次。

- ALIASES_DICT：别名 -> 规范名（agent 用于病名归一）
- DISEASE_ALIASES：规范名 -> 别名列表（main 用于医保条款匹配）
- DISEASE_VARIANTS：10 条结构性变体（截断/缩写），不是完整同义词库
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medical_aliases.json")

ALIASES_DICT: dict[str, str] = {}
DISEASE_ALIASES: dict[str, list[str]] = {}
ABBREVIATIONS: dict[str, str] = {}

if os.path.exists(_ALIASES_PATH):
    try:
        with open(_ALIASES_PATH, "r", encoding="utf-8") as f:
            _raw = json.load(f)
        for standard_name, data in _raw.items():
            ALIASES_DICT[standard_name.lower()] = standard_name
            aliases = [str(a) for a in data.get("aliases", []) if a]
            abbrs = [str(a) for a in data.get("abbreviations", []) if a]
            DISEASE_ALIASES[standard_name] = aliases + abbrs
            for alias in aliases + abbrs:
                ALIASES_DICT[alias.lower()] = standard_name
            for abbr in abbrs:
                ABBREVIATIONS[abbr] = standard_name
                ABBREVIATIONS[abbr.lower()] = standard_name
        logger.info(f"✅ 医学字典加载成功，共 {len(ALIASES_DICT)} 个同义词映射。")
    except Exception as e:
        logger.error(f"❌ 医学字典加载失败: {e}")

# 长的缩写先替换，避免 NSCLC 被 SCLC 规则误伤
ABBREVIATIONS = dict(sorted(ABBREVIATIONS.items(), key=lambda kv: -len(kv[0])))

# 截断/缩写类变体；不再把 非小细胞肺癌/小细胞肺癌 都映射成“肺癌”（会互相覆盖）
DISEASE_VARIANTS: dict[str, tuple[str, ...]] = {
    "肝癌": ("肝细胞癌",),
    "肝细胞癌": ("肝癌",),
    "头颈鳞癌": ("头颈",),
    "头颈部鳞状细胞癌": ("头颈",),
    "恶性胸膜间皮瘤": ("间皮瘤",),
    "尿路上皮癌": ("尿路上皮",),
    "胃癌": ("胃或胃食管",),
    "食管癌": ("食管",),
    "非小细胞肺癌": ("NSCLC",),
    "小细胞肺癌": ("SCLC",),
}
