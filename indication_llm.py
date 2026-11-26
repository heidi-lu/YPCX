"""LLM 适应症归一（不依赖 medical_aliases / _CONCISE_RULES）。

只做“从全文到简称/分型”的判断，不写库。规则：
- 疾病本身有公认分型 → short=疾病名，subtype=分型全称；
- 疾病没有公认分型 → short=疾病全称，subtype=short；
- 活动性/严重度/年龄/抗体/线数/联合方案/术前术后都不算分型。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from json_repair import repair_json

import llm_client

BAD_WORDS = ("患者", "联用", "Keytruda", "疾病进展", "适用于", "用于")
CATEGORY_SUFFIX_RE = re.compile(r"(癌|瘤|病|炎|综合征|肌无力|哮喘|息肉|感染|疮|紫癜|硬化|纤维化)$")
# 分期/线数/活动度/严重度不是“疾病分型”，不能进 subtype
STAGE_MARKERS = (
    "局部晚期", "晚期", "转移性", "复发", "不可切除", "可手术", "新辅助",
    "术后", "术前", "一线", "二线", "三线", "活动性", "中重度", "重度",
    "轻度", "嗜酸性粒细胞升高",
)
STAGE_RE = re.compile(r"[ⅠⅡⅢⅣIVXivx0-9]+期")
PROMPT_HEAD = """你是药品适应症归一化器。不要使用任何病名别名词典、映射表或硬编码词表；只根据给定的全文和医学常识判断。

规则：
1. 先找“最上层标准大类” short：如 肺癌、膀胱癌、乳腺癌、肝癌、胃癌、食管癌、重症肌无力、淋巴瘤、软组织肉瘤、头颈部癌。
2. 再找“原文中的具体疾病/亚型全称” subtype：如 非小细胞肺癌、肌层浸润性膀胱癌、全身型重症肌无力、三阴性乳腺癌。
3. 如果原文只有大类本身，或没有更细的亚型：short = subtype = 疾病全称（如 系统性红斑狼疮、类风湿关节炎、干燥综合征）。
4. 活动性、分期、转移、复发、一线/二线、术前/术后、中重度都不算 subtype，不能放进 subtype。
5. 示例：非小细胞肺癌 → short=肺癌，subtype=非小细胞肺癌；肌层浸润性膀胱癌 → short=膀胱癌，subtype=肌层浸润性膀胱癌；全身型重症肌无力 → short=重症肌无力，subtype=全身型重症肌无力；结外NK/T细胞淋巴瘤 → short=淋巴瘤，subtype=结外NK/T细胞淋巴瘤；原发性免疫球蛋白A肾病 → short=IgA肾病，subtype=原发性免疫球蛋白A肾病；腺泡状软组织肉瘤 → short=软组织肉瘤，subtype=腺泡状软组织肉瘤；嗜酸性肉芽肿性多血管炎 → short=血管炎，subtype=嗜酸性肉芽肿性多血管炎；慢性鼻窦炎伴鼻息肉 → short=鼻窦炎，subtype=慢性鼻窦炎伴鼻息肉；错配修复缺陷型或微卫星高度不稳定型实体瘤 → short=实体瘤，subtype=错配修复缺陷型或微卫星高度不稳定型实体瘤；默克尔细胞癌 → short=皮肤癌，subtype=默克尔细胞癌；系统性红斑狼疮 → short=subtype=系统性红斑狼疮。
6. 如果原文里出现了比大类更具体的病名，subtype 必须用那个具体病名，不能把 subtype 填成大类。例如原文是“结外NK/T细胞淋巴瘤”，不能输出“淋巴瘤 / 淋巴瘤”，必须输出“淋巴瘤 / 结外NK/T细胞淋巴瘤”。
7. 每个字段必须有原文依据；不要编造。找不到就留空字符串。
8. 只输出 JSON：{{"items":[{{"id":97,"disease":"","has_subtypes":false,"subtype_full":"","short":"","subtype":"","evidence":""}}]}}

输入：
"""


@dataclass
class Normalized:
    id: int
    disease: str
    has_subtypes: bool
    subtype_full: str
    short: str
    subtype: str
    evidence: str


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _valid(text: str, full: str) -> bool:
    value = _norm(text)
    if not value or len(value) > 30:
        return False
    if any(word in value for word in BAD_WORDS):
        return False
    if value in {"疾病", "病情", "病变"} or value.endswith("高疾病") or value.startswith(("降低存在", "存在疾病")):
        return False
    return value in _norm(full)


def _valid_short(short: str, full: str, subtype: str) -> bool:
    """简称可以是原文里没有逐字出现的“大类”，如 肝细胞癌 → 肝癌。"""
    value = _norm(short)
    if not _valid(value, full):
        if len(value) > 8 or not CATEGORY_SUFFIX_RE.search(value):
            return False
        if not _valid(subtype, full):
            return False
    return True


def normalize(rows: list[dict], model: str = "qwen3.8-flash", thinking: bool = True, timeout: int = 120, max_attempts: int = 1):
    """rows: [{id, generic_name, full}]；返回 ({id: Normalized}, LLMResult|None)。"""
    lines = [f"id={row['id']} | 通用名={row.get('generic_name','')} | 全文={row.get('full','')}" for row in rows]
    prompt = PROMPT_HEAD + "\n".join(lines)
    result = llm_client.chat(
        [{"role": "user", "content": prompt}],
        model=model,
        stage="think",
        thinking=thinking,
        json_mode=True,
        max_tokens=6000,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    if not result:
        return {}, None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.text.strip(), flags=re.M)
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start:end + 1])
    except Exception:
        data = json.loads(repair_json(raw[start:end + 1]))

    by_id = {int(row["id"]): row for row in rows}
    output: dict[int, Normalized] = {}
    for item in data.get("items", []):
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        row = by_id.get(item_id)
        if not row:
            continue
        full = str(row.get("full") or "")
        short = str(item.get("short") or "").strip()
        subtype = str(item.get("subtype") or "").strip()
        has_subtypes = bool(item.get("has_subtypes"))
        if not _valid_short(short, full, subtype):
            continue
        # 以模型给出的 short/subtype 为准；has_subtypes 只作参考，
        # 避免把“IgA肾病 / 原发性免疫球蛋白A肾病”这种正确拆分强行压成同一值。
        if subtype and _norm(subtype) != _norm(short):
            if not subset_valid(subtype, short, full):
                # 模型把分期/活动度当分型：退回“不拆分”，用疾病全称
                disease = str(item.get("disease") or "").strip()
                if _valid(disease, full):
                    short = disease
                    subtype = disease
                    has_subtypes = False
                else:
                    continue
        else:
            subtype = short
        output[item_id] = Normalized(
            id=item_id,
            disease=str(item.get("disease") or "").strip(),
            has_subtypes=has_subtypes,
            subtype_full=str(item.get("subtype_full") or "").strip(),
            short=short,
            subtype=subtype,
            evidence=str(item.get("evidence") or "").strip(),
        )
    return output, result


def subset_valid(subtype: str, short: str, full: str) -> bool:
    value = _norm(subtype)
    if not value or len(value) > 30:
        return False
    if any(word in value for word in BAD_WORDS):
        return False
    if any(word in value for word in STAGE_MARKERS) or STAGE_RE.search(value):
        return False
    if value == _norm(short):
        return False
    return value in _norm(full)


def normalize_products(products: list[dict], model: str | None = None, thinking: bool | None = None,
                       batch_size: int = 5) -> dict:
    """把 agent 的 products 结构交给 LLM 归一；只改 indications，不碰其他字段。

    products: [{generic_name, indications:[{indication_full, indication_short,...}]}]
    返回统计：updated / failed / cost_cny / batches。
    """
    from config import LLM_INDICATION_MODEL, LLM_INDICATION_THINKING

    model = model or LLM_INDICATION_MODEL
    thinking = LLM_INDICATION_THINKING if thinking is None else bool(thinking)
    rows: list[dict] = []
    mapping: dict[int, tuple[int, int]] = {}
    for pi, product in enumerate(products):
        for ii, ind in enumerate(product.get("indications") or []):
            full = str(ind.get("indication_full") or ind.get("indication_short") or "").strip()
            if not full:
                continue
            idx = len(rows)
            rows.append({"id": idx, "generic_name": product.get("generic_name", ""), "full": full})
            mapping[idx] = (pi, ii)

    normalized: dict[int, Normalized] = {}
    cost = 0.0
    batches = 0
    for offset in range(0, len(rows), max(1, batch_size)):
        chunk = rows[offset:offset + max(1, batch_size)]
        batches += 1
        part, result = normalize(chunk, model=model, thinking=thinking)
        normalized.update(part)
        if result:
            cost += float(result.cost_cny or 0.0)

    for idx, item in normalized.items():
        pi, ii = mapping[idx]
        ind = products[pi]["indications"][ii]
        ind["indication_short"] = item.short
        ind["indication_subtype"] = item.subtype
        ind["_llm_normalized"] = True
        if item.evidence:
            ind["indication_short_evidence"] = item.evidence

    failed = [idx for idx in mapping if idx not in normalized]
    return {"updated": len(normalized), "failed": len(failed), "cost_cny": round(cost, 6), "batches": batches}
