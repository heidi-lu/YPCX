import json
import asyncio
import os
import re
import unicodedata
import logging
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import datetime, timedelta

from config import QUERY_CONCURRENCY, QUERY_MAX_BATCH, INDICATION_DICTS, INSURANCE_ALIASES, LLM_OVERWRITE_APPROVED
from db import init_db, get_db, SessionLocal
from models import Drug, Feedback
import usage_store
from agent import fetch_new_drug, update_existing_drug, normalize_manufacturer, normalize_specification, _normalize_disease_with_dict
from disease_terms import DISEASE_ALIASES, DISEASE_VARIANTS

os.environ["ANYIO_BACKEND"] = "asyncio"

logger = logging.getLogger(__name__)

# 人工补全时不允许改的字段（PDF/医保/指南等权威字段）
LOCKED_EDIT_FIELDS = {
    "china_approval_date", "indication_full", "drug_insurance_date", "insurance_status_text",
    "guideline_name", "guideline_recommendation", "nhsa_pdf_url", "nhsa_ppt_url",
    "insurance_category", "insurance_restriction", "dosage_form", "specification",
}

app = FastAPI(title="药品信息查询")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

NHI_DICT, _NHI_INDEX, _NHI_ALIAS, _NHI_ALIAS_NAME, _NHI_NORM, _NHI_NORM_NAME = {}, {}, {}, {}, {}, {}


def _norm_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", text)


def _load_dict():
    global NHI_DICT, _NHI_INDEX, _NHI_ALIAS, _NHI_ALIAS_NAME, _NHI_NORM, _NHI_NORM_NAME
    if os.path.exists("nhi_dictionary.json"):
        with open("nhi_dictionary.json", "r", encoding="utf-8") as f: NHI_DICT = json.load(f)
        for k, value in NHI_DICT.items():
            name = k.split("|")[0] if "|" in k else k
            _NHI_INDEX.setdefault(name, []).append(k)
            _NHI_NORM[_norm_key(k)] = k
            _NHI_NORM_NAME.setdefault(_norm_key(name), []).append(k)
            form = value.get("dosage_form", "") if isinstance(value, dict) else ""
            for alias in (value.get("aliases", []) if isinstance(value, dict) else []):
                alias_name = str(alias).strip()
                if alias_name:
                    _NHI_ALIAS.setdefault(_norm_key(f"{alias_name}|{form}"), k)
                    _NHI_ALIAS_NAME.setdefault(_norm_key(alias_name), []).append(k)
            for alias_key in (value.get("alias_keys", []) if isinstance(value, dict) else []):
                alias_key = str(alias_key).strip()
                if alias_key:
                    _NHI_ALIAS.setdefault(_norm_key(alias_key), k)



_load_dict()
_save_lock = asyncio.Lock()

@app.on_event("startup")
def startup():
    init_db()
    _cleanup_drafts()


def _normalize_dosage_form(raw_form: str) -> str:
    if not raw_form: return ""
    if "注射" in raw_form or "针" in raw_form: return "注射剂"
    if "片" in raw_form: return "片剂"
    if "胶囊" in raw_form: return "胶囊"
    if "口服液" in raw_form or "液体" in raw_form: return "口服液体剂"
    if "颗粒" in raw_form: return "颗粒剂"
    if "乳膏" in raw_form or "软膏" in raw_form: return "软膏剂"
    if "植入" in raw_form or raw_form.strip() == "装置": return "植入剂"
    return raw_form.strip()

def _match_nhi(generic_name: str, dosage_form: str = ""):
    if not generic_name or not NHI_DICT: return None
    raw_name = str(generic_name).strip()
    norm_name = _norm_key(raw_name)
    clean_form = _normalize_dosage_form(dosage_form)

    if clean_form:
        exact_key = f"{raw_name}|{clean_form}"
        if exact_key in NHI_DICT: return NHI_DICT[exact_key]
        norm_key = f"{norm_name}|{_norm_key(clean_form)}"
        if norm_key in _NHI_NORM: return NHI_DICT[_NHI_NORM[norm_key]]
        if norm_key in _NHI_ALIAS: return NHI_DICT[_NHI_ALIAS[norm_key]]

    # 旧名/别名查询：只在唯一命中时返回，避免一个旧名跨多个剂型被乱配
    alias_keys = _NHI_ALIAS_NAME.get(norm_name, [])
    if len(set(alias_keys)) == 1:
        return NHI_DICT[alias_keys[0]]

    for k, v in NHI_DICT.items():
        if _norm_key(k.split("|")[0]) == norm_name: return v

    clean_name = re.sub(r'[（(].*?[）)]', '', raw_name).strip()
    for k, v in NHI_DICT.items():
        dict_name = re.sub(r'[（(].*?[）)]', '', k.split("|")[0]).strip()
        if clean_name in dict_name or dict_name in clean_name: return v
    return None


def _term_in_text(term: str, text: str) -> bool:
    term = str(term or "").strip()
    if not term:
        return False
    if re.fullmatch(r"[A-Za-z0-9\-]+", term):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.I))
    return term in text


def _disease_terms(raw: str) -> set[str]:
    canonical = _normalize_disease_with_dict(raw) or str(raw or "").strip()
    terms = {canonical, str(raw or "").strip()}
    for alias in DISEASE_ALIASES.get(canonical, []):
        terms.add(alias)
    return {t for t in terms if t}


def _resolve_brand(base_brand: str, ind_brand: str) -> str:
    base = str(base_brand or "").strip()
    ind = str(ind_brand or "").strip()
    if not ind:
        return base
    if not base:
        return ind
    # 已有产品品牌优先；只在产品品牌为空时使用适应症品牌
    return base


def _display_indication(raw: str) -> tuple[str, str]:
    """返回 (展示用大类, 细分)。哮喘按大类展示，但保留 SEA/过敏性等细分。"""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return "", ""
    # 非小细胞肺癌/小细胞肺癌必须保留区分：别名字典会把两者都归一成“肺癌”
    if "非小细胞肺癌" in text:
        return "非小细胞肺癌", ""
    if "小细胞肺癌" in text:
        return "小细胞肺癌", ""
    specific = _normalize_disease_with_dict(text) or text
    upper = specific.upper()
    if "哮喘" in specific or "SEA" in upper or "ASTHMA" in upper:
        return "哮喘", specific
    if "COPD" in upper or "慢性阻塞" in specific or "慢阻肺" in specific:
        return "慢性阻塞性肺疾病", ""
    if "CRSWNP" in upper or "慢性鼻窦炎伴鼻息肉" in specific:
        return "慢性鼻窦炎伴鼻息肉", ""
    if "EGPA" in upper or "嗜酸性肉芽肿" in specific:
        return "嗜酸性肉芽肿性多血管炎", ""
    return specific, ""


_SUBTYPE_HEADING = {"适应症", "功能主治", "联合治疗", "单药治疗", "用法用量", "治疗方案"}
_SUBTYPE_BRACKET_RE = re.compile(r"[【\[]([^】\]]{2,30})[】\]]")
_SUBTYPE_SUFFIX = re.compile(r"癌|瘤|病|炎|哮喘|息肉|综合征|肌无力|感染")
_STAGING_PREFIX = (
    "复发或转移性", "不可切除或转移性", "局部晚期或转移性", "转移性", "复发性",
    "局部晚期", "晚期", "早期", "不可切除", "不可手术切除", "经典型", "围术期", "新辅助", "辅助",
)
# (匹配, 最简简称, 分型) —— 只做结构性的“大类/细分”，不是完整同义词
_CONCISE_RULES = (
    (r"非鳞状非小细胞肺癌", "肺癌", "非鳞状非小细胞肺癌"),
    (r"鳞状非小细胞肺癌", "肺癌", "鳞状非小细胞肺癌"),
    (r"非小细胞肺癌", "肺癌", "非小细胞肺癌"),
    (r"小细胞肺癌", "肺癌", "小细胞肺癌"),
    (r"肝细胞癌", "肝癌", "肝细胞癌"),
    (r"三阴性乳腺癌", "乳腺癌", "三阴性乳腺癌"),
    (r"食管鳞状细胞癌", "食管癌", "食管鳞状细胞癌"),
    (r"胃(?:或|及)胃食管(?:交界处|结合部)腺癌", "胃癌", "胃或胃食管结合部腺癌"),
    (r"过敏性哮喘", "哮喘", "过敏性哮喘"),
    (r"重度[^，。；]{0,12}哮喘", "哮喘", ""),
)
_QUALIFIER_HINTS = (("术前", "术前"), ("术后", "术后"), ("新辅助", "新辅助"), ("围术期", "围术期"), ("辅助", "辅助"))


def _split_short_subtype(raw: str, full: str = "") -> tuple[str, str]:
    """把适应症拆成“最简简称 + 分型”。

    分型候选：全文【】/[] 里的病名、原文中的细分写法、术前/术后/新辅助等时机；
    简称取最简大类（肺癌/肝癌/胃癌…），分型保留细分（非小细胞肺癌/肝细胞癌…）。
    """
    text = re.sub(r"\s+", "", str(raw or ""))
    if not text:
        return "", ""
    full_text = re.sub(r"\s+", "", str(full or ""))
    candidates = []
    for m in _SUBTYPE_BRACKET_RE.finditer(full_text):
        inner = m.group(1).strip()
        if inner in _SUBTYPE_HEADING or not _SUBTYPE_SUFFIX.search(inner):
            continue
        candidates.append(inner)
        break

    short = text
    matched = False
    if INDICATION_DICTS:
        for pattern, concise, sub in _CONCISE_RULES:
            if re.search(pattern, text):
                short, matched = concise, True
                if sub:
                    candidates.append(sub)
                break
        if not matched:
            for pattern, concise, sub in _CONCISE_RULES:
                if re.search(pattern, full_text):
                    short, matched = concise, True
                    if sub:
                        candidates.append(sub)
                    break

    if text != short and _SUBTYPE_SUFFIX.search(text):
        candidates.append(text)
    if full_text and short:
        for m in re.finditer(rf"([\u4e00-\u9fa5A-Za-z0-9\-]{{2,12}}?{re.escape(short)})", full_text):
            phrase = m.group(1)
            for marker in ("本品", "适用于", "用于", "治疗", "联合", "的"):
                idx = phrase.rfind(marker)
                if idx >= 0:
                    phrase = phrase[idx + len(marker):]
            phrase = re.sub(r"^[ⅠⅡⅢⅣIVXivx0-9A-Za-z\-]+期", "", phrase)
            if (len(phrase) > len(short) and _SUBTYPE_SUFFIX.search(phrase)
                    and not re.search(r"患者|成人|儿童", phrase)):
                candidates.append(phrase)
                break

    for word, label in _QUALIFIER_HINTS:
        if re.search(word, text) or re.search(word, full_text):
            candidates.append(label)
            break

    for prefix in _STAGING_PREFIX:
        if short.startswith(prefix):
            short = short[len(prefix):]
            break

    cleaned = []
    for cand in candidates:
        cand = re.sub(r"^[、，,或和及\s]+", "", str(cand or "").strip())
        for _ in range(2):
            cand = re.sub(r"^[ⅠⅡⅢⅣIVXivx0-9A-Za-z\-]*期", "", cand)
            cand = re.sub(r"^[、，,或和及\s]+", "", cand)
        cand = cand.strip()
        if cand and cand != short and _SUBTYPE_SUFFIX.search(cand):
            cleaned.append(cand)
    best = max(cleaned, key=len) if cleaned else ""
    quals = []
    for word, label in _QUALIFIER_HINTS:
        if (word in text or word in full_text) and label not in best:
            quals.append(label)
            break
    if quals:
        best = "、".join(([best] if best else []) + quals)
    return short, best


def _derive_insurance_year(nhi_data: dict, ind_short: str, approval_date: str, is_generic: bool = False, p0_ins_year: str = "") -> str:
    """医保目录是权威来源：先匹配目录限制，不用获批时间去否定目录年份。"""
    if not nhi_data:
        return "未纳入医保"
    timeline = nhi_data.get("restriction_timeline", {})
    execution_dates = nhi_data.get("execution_dates", {})
    if not isinstance(execution_dates, dict):
        execution_dates = {}
    if not timeline:
        return "未纳入医保"

    valid_years = sorted([y for y in timeline.keys() if y.isdigit()], key=int)
    if not valid_years:
        return "未纳入医保"

    app_year = 0
    if approval_date:
        m = re.search(r"(\d{4})", str(approval_date))
        if m:
            app_year = int(m.group(1))

    terms = _disease_terms(ind_short)
    for year in valid_years:
        restriction = re.sub(r"\s+", "", str(timeline.get(year, "")))
        if restriction == "无限制":
            # 无限制只代表目录没限定；没有获批时间时不能硬套
            if app_year and int(year) >= app_year:
                return execution_dates.get(year, year)
            continue
        if any(_term_in_text(term, restriction) for term in terms):
            return execution_dates.get(year, year)
    return "未纳入医保"


def _restriction_clauses(restriction: str) -> list[str]:
    text = re.sub(r"^限\s*[：:]?", "", str(restriction or "").strip())
    if not text:
        return []
    if re.search(r"(?<!\d)1\s*[.、]", text):
        parts = re.split(r"(?=\d+\s*[.、])", text)
    else:
        parts = re.split(r"[；;]", text)
    return [p.strip() for p in parts if p.strip()]


_GENERIC_DISEASE_NAMES = {"肺癌", "恶性肿瘤", "皮炎"}


def _status_terms(full: str, short: str = "") -> list[str]:
    """从适应症原文+简称里抽取能用于匹配医保限制的病名。

    以前只认十几个写死的癌种，导致特应性皮炎、宫颈癌、视神经脊髓炎这类
    明明在目录里的适应症全部显示“未纳入”。现在改为三层：
    1) 历史硬编码关键词（保留原有胃/食管特判）
    2) 简称走医学别名字典（`_disease_terms`，覆盖哮喘、结节性痒疹等）
    3) 全文扫描别名字典 + 通用“…癌/…瘤”兜底（覆盖宫颈癌等未收录病名）
    """
    terms = []
    for key in ("霍奇金", "尿路上皮", "非小细胞肺癌", "小细胞肺癌", "肝细胞癌", "微卫星", "鼻咽"):
        if key in full:
            terms.append(key)
    # “胃或胃食管”里的“食管”不是食管癌，不能把胃癌匹配成食管癌
    if "胃或胃食管" in full or "胃及胃食管" in full:
        terms.append("胃")
    else:
        if "食管" in full:
            terms.append("食管")
        if "胃" in full:
            terms.append("胃")

    if short:
        terms.extend(_disease_terms(short))

    if INSURANCE_ALIASES:
        for canonical, aliases in DISEASE_ALIASES.items():
            if canonical in _GENERIC_DISEASE_NAMES:
                continue
            if canonical in full or any(alias in full for alias in aliases):
                terms.append(canonical)

    for match in re.finditer(r"[\u4e00-\u9fa5]{2,9}(?:癌|淋巴瘤|白血病|骨髓瘤|肉瘤)", full):
        terms.append(match.group(0))

    seen, result = set(), []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def _clause_conflicts(clause: str, full: str) -> bool:
    """病名相同但限定词冲突时不算同一条适应症。

    只做“原文明确写了两边”的互斥判断，宁缺毋滥：
    - 鳞状 / 非鳞状 互斥；
    - 联合维迪西妥或 HER2 的新适应症，旧条款不含 HER2 就不是同一条。
    """
    c, f = str(clause), str(full)
    f_nonsq = "非鳞" in f
    f_sq = ("鳞状" in f or "鳞癌" in f) and not f_nonsq
    c_nonsq = "非鳞" in c
    c_sq = ("鳞状" in c or "鳞癌" in c) and not c_nonsq
    if f_nonsq and c_sq:
        return True
    if f_sq and c_nonsq:
        return True
    if "维迪西妥" in f or "HER2" in f.upper():
        if not ("维迪西妥" in c or "HER2" in c.upper()):
            return True
    return False


def _clause_matches_indication(clause: str, full: str, short: str = "") -> bool:
    terms = _status_terms(full, short)
    if not any(term in clause for term in terms):
        return False
    if _clause_conflicts(clause, full):
        return False
    if "小细胞肺癌" in full and "非小细胞肺癌" not in full and "非小细胞肺癌" in clause:
        return False
    if "非小细胞肺癌" in full and "非小细胞肺癌" not in clause and "小细胞肺癌" in clause:
        return False
    if "非小细胞肺癌" in full:
        if "既往接受过" in full and "既往" not in clause:
            return False
        if ("新辅助" in full or "可切除" in full or "围术期" in full):
            if not any(x in clause for x in ("新辅助", "可切除", "围术期", "辅助治疗")):
                return False
    if "食管" in full:
        if "既往" in full and "既往" not in clause:
            return False
        if "一线" in full and "既往" not in full and ("既往" in clause or "进展" in clause):
            return False
    if "一线" in full and "一线" not in clause:
        return False
    return True


def _insurance_status_text(nhi_data: dict, ind_full: str, ind_short: str) -> str:
    """逐年匹配医保目录，输出“2021年进入，2026年退出”。"""
    if not nhi_data:
        return "未纳入"
    timeline = nhi_data.get("restriction_timeline", {})
    if not isinstance(timeline, dict) or not timeline:
        return "未纳入"
    years = sorted([int(y) for y in timeline if str(y).isdigit()])
    if not years:
        return "未纳入"

    full = str(ind_full or "")
    present = []
    for year in years:
        clauses = _restriction_clauses(timeline.get(str(year), ""))
        if any(_clause_matches_indication(c, full, ind_short) for c in clauses):
            present.append(year)

    if not present:
        return "未纳入"
    first, last = present[0], present[-1]
    max_year = max(years)
    if last >= max_year:
        return f"{first}年进入"
    return f"{first}年进入，{last + 1}年退出"


def _indication_text_key(value) -> str:
    return re.sub(r"[\s（）()]+", "", str(value or ""))


_SHORT_BAD_PREFIXES = ("联合", "单药", "本品", "适用于", "用于", "至少经过")
_SHORT_BAD_TOKENS = ("患者", "联用", "Keytruda", "鳞状", "腺癌", "细胞癌", "结合部", "非鳞状")


def _is_bad_short(short: str) -> bool:
    """简称是否是碎片/长句；用于回填时判断要不要用新值覆盖旧值。"""
    text = str(short or "").strip()
    if not text or len(text) > 20:
        return True
    if text in {"疾病", "病情", "病变"} or text.endswith("高疾病") or text.startswith(("在常规治疗", "适用于降低")):
        return True
    if text.startswith(_SHORT_BAD_PREFIXES):
        return True
    return any(token in text for token in _SHORT_BAD_TOKENS)


def _is_bad_subtype(subtype: str) -> bool:
    text = str(subtype or "").strip()
    if not text:
        return False
    return len(text) > 30 or text.startswith(("降低存在", "适用于", "用于")) or any(
        token in text for token in ("患者", "联用")
    )


def _save_rows(name: str, rows: list) -> list:
    with SessionLocal() as s:
        seen = set()
        touched = set()
        for row in rows:
            for k, v in row.items():
                if isinstance(v, str):
                    v = re.sub(r'[\x00-\x1F\x7F]', '', v).strip()
                    if k == "brand_name" and re.search(r'[^\w\u4e00-\u9fa5®™\-\s]', v): v = ""
                    if k == "manufacturer":
                        v = normalize_manufacturer(v)
                    if k == "specification":
                        v = normalize_specification(v)
                    row[k] = v

            k = (row.get("generic_name",""), row.get("brand_name",""), row.get("manufacturer",""),
                 row.get("dosage_form",""), row.get("specification",""),
                 row.get("indication_short",""), row.get("indication_subtype",""),
                 _indication_text_key(row.get("indication_full")))
            if k in seen: continue
            seen.add(k)
            
            ex = s.query(Drug).filter(
                Drug.generic_name==k[0], Drug.brand_name==k[1], Drug.manufacturer==k[2],
                Drug.dosage_form==k[3], Drug.specification==k[4],
                Drug.indication_short==k[5], Drug.indication_subtype==k[6],
                Drug.indication_full == row.get("indication_full", "")
            ).first()
            
            if not ex:
                # 按“通用名+厂家+剂型+规格+全文”找已有行：简称改了也要更新旧行，而不是新建重复行
                target_text = _indication_text_key(row.get("indication_full"))
                if target_text:
                    candidates = s.query(Drug).filter(
                        Drug.generic_name == k[0], Drug.brand_name == k[1], Drug.manufacturer == k[2],
                        Drug.dosage_form == k[3], Drug.specification == k[4],
                    ).all()
                    # 优先匹配待审核行，其次才是已入库行
                    candidates.sort(key=lambda item: 0 if item.status == "DRAFT" else 1)
                    for candidate in candidates:
                        if _indication_text_key(candidate.indication_full) == target_text:
                            ex = candidate
                            break

            if ex:
                # 已入库行：只修复明显不合格的适应症简称，完整原文不动
                old_short = str(ex.indication_short or "").strip()
                new_short = str(row.get("indication_short") or "").strip()
                bad_old = _is_bad_short(old_short)
                if (ex.status == "APPROVED" and row.get("_llm_indication") and LLM_OVERWRITE_APPROVED
                        and new_short and not _is_bad_short(new_short)):
                    ex.indication_short = new_short
                    ex.indication_subtype = str(row.get("indication_subtype") or "").strip()
                elif ex.status == "APPROVED" and bad_old and new_short and not _is_bad_short(new_short):
                    ex.indication_short = new_short
                elif ex.status == "DRAFT" and row.get("_llm_indication") and new_short and not _is_bad_short(new_short):
                    # 大模型归一的结果：DRAFT 行直接采用（包括把“全身型重症肌无力”改成“重症肌无力”）
                    ex.indication_short = new_short
                    ex.indication_subtype = str(row.get("indication_subtype") or "").strip()
                elif ex.status == "DRAFT":
                    # 待审核行：旧值是碎片/长句时用新值覆盖；旧值已经合格就不动
                    if new_short and (not old_short or bad_old) and not _is_bad_short(new_short):
                        ex.indication_short = new_short
                    old_sub = str(ex.indication_subtype or "").strip()
                    new_sub = str(row.get("indication_subtype") or "").strip()
                    if new_sub and (not old_sub or _is_bad_subtype(old_sub)):
                        ex.indication_subtype = new_sub
                    elif old_sub and _is_bad_subtype(old_sub) and not new_sub:
                        ex.indication_subtype = ""

                # 其余已有行只补空字段，绝不用新值覆盖已有值
                for ck, v in row.items():
                    if not hasattr(Drug, ck) or v in (None, ""):
                        continue
                    if getattr(ex, ck) in (None, ""):
                        setattr(ex, ck, v)
                if ex.status == "DRAFT":
                    ex.updated_at = datetime.now()
                touched.add(ex.id)
            else: 
                safe_row = {ck: cv for ck, cv in row.items() if hasattr(Drug, ck) and cv is not None}
                created = Drug(**safe_row)
                s.add(created)
                s.flush()
                touched.add(created.id)
        s.commit()
        # 只返回本次查询写入/命中的行：旧待审核行仍留在库里，但不混进当前结果
        results = s.query(Drug).filter(Drug.id.in_(touched)).all() if touched else []
        return [_drug_to_dict(d) for d in results]

def _insurance_price_text(nhi_data: dict) -> str:
    if not nhi_data:
        return ""
    price = str(nhi_data.get("price_medical_insurance") or "").strip()
    return "" if price in {"", "*"} else price


def _ym_tuple(value):
    m = re.search(r"(\d{4})(?:[^\d]*(\d{1,2}))?", str(value or ""))
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2) or 1)
    if not 1900 <= year <= 2100 or not 1 <= month <= 12:
        return None
    return (year, month)


def _clamp_indication_approval_date(ind_date: str, china_date: str, country: str) -> str:
    """适应症获批不可能早于药品首次上市；早于的视为误抽，宁可不写。

    以前用上市时间兜底，会把假日期写进库；现在按方案 A 留空，前端显示“未公开”。
    """
    if country != "中国" or not ind_date or not china_date:
        return ind_date
    ind, first = _ym_tuple(ind_date), _ym_tuple(china_date)
    if not ind or not first or ind >= first:
        return ind_date
    logger.info(f"[时间校验] 适应症获批时间 {ind_date} 早于首次上市 {china_date}，留空待核实")
    return ""


def _optional_text(value) -> str:
    text = str(value or "").strip()
    return "" if text in {"未提供", "未提及", "未知", "暂无", "无"} else text


def _country_dedupe_rows(rows: list[dict]) -> list[dict]:
    """同一药品同一适应症，中国获批优先，去掉美国/海外重复行。"""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        short = _normalize_disease_with_dict(row.get("indication_short", ""))
        # 鼻窦炎是上位名，和“慢性鼻窦炎伴鼻息肉”合并成同一组
        if short == "鼻窦炎":
            short = "慢性鼻窦炎伴鼻息肉"
        # 同一疾病的不同中文写法
        if short == "结节性瘙痒症":
            short = "结节性痒疹"
        key = (
            str(row.get("generic_name", "")).strip(),
            str(row.get("manufacturer", "")).strip(),
            short,
            str(row.get("indication_subtype") or "").strip(),
        )
        groups.setdefault(key, []).append(row)

    result = []
    for group in groups.values():
        china_rows = [r for r in group if r.get("approval_country") == "中国"]
        if china_rows:
            result.extend(china_rows)
            continue
        # 没有中国行时，海外只保留最具体的一条，避免上位名/子类重复
        result.append(max(group, key=lambda r: len(str(r.get("indication_full", "")))))
    result = _drop_covered_overseas_rows(result)
    result = _drop_web_covered_rows(result)
    return _drop_web_full_text_duplicates(result)


_OVERSEAS_DISEASE_HINTS = (
    "非小细胞肺癌", "小细胞肺癌", "霍奇金", "尿路上皮", "肝细胞癌", "食管", "鼻咽",
    "胃", "微卫星", "宫颈", "乳腺", "结直肠", "黑色素瘤", "头颈", "卵巢", "胰腺",
    "前列腺", "肾细胞", "甲状腺", "骨髓瘤", "白血病", "肉瘤", "间皮瘤", "胆道",
    "特应性皮炎", "结节性痒疹", "慢性阻塞性肺疾病", "鼻窦炎", "荨麻疹",
    "嗜酸性食管炎", "大疱性类天疱疮", "重症肌无力", "视神经脊髓炎", "IgG4",
    "银屑病", "克罗恩", "溃疡性结肠炎", "哮喘",
)


def _drop_covered_overseas_rows(rows: list[dict]) -> list[dict]:
    """海外行如果只是中国已覆盖的同一类病，或干脆是“恶性肿瘤”这种泛指，就丢掉。"""
    china_text = "".join(
        str(r.get("indication_short", "")) + str(r.get("indication_full", ""))
        for r in rows if r.get("approval_country", "中国") == "中国"
    )
    if not china_text:
        return rows
    china_subtypes = {
        str(r.get("indication_subtype") or "") for r in rows if r.get("approval_country", "中国") == "中国"
    }
    result = []
    for row in rows:
        if row.get("approval_country", "中国") == "中国":
            result.append(row)
            continue
        subtype = str(row.get("indication_subtype") or "")
        if subtype and subtype in china_subtypes:
            continue
        short = str(row.get("indication_short", ""))
        keys = [h for h in _OVERSEAS_DISEASE_HINTS if h in short] if INDICATION_DICTS else []
        if keys and all(k in china_text for k in keys):
            continue
        if not keys and len(short) <= 6:
            continue
        result.append(row)
    return result


_DOSAGE_SUFFIX_RE = re.compile(r"(?:注射液|注射剂|片剂|片|胶囊|颗粒|口服液|滴丸|丸|散|乳膏|软膏|凝胶|喷雾剂|吸入剂|贴剂|栓|酊|膏|水|液)$")
def _generic_core(name: str) -> str:
    return _DOSAGE_SUFFIX_RE.sub("", str(name or "").strip())


def _drop_web_covered_rows(rows: list[dict]) -> list[dict]:
    """网搜产品补出来的适应症，若已被同通用名的官方档案行覆盖，就不重复显示。"""
    official: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("nhsa_pdf_url") or row.get("nhsa_ppt_url"):
            official.setdefault(_generic_core(row.get("generic_name")), []).append(row)
    if not official:
        return rows
    result = []
    for row in rows:
        if row.get("nhsa_pdf_url") or row.get("nhsa_ppt_url"):
            result.append(row)
            continue
        core = _generic_core(row.get("generic_name"))
        official_rows = official.get(core)
        if not official_rows:
            result.append(row)
            continue
        if row.get("origin_type") == "仿制":
            result.append(row)
            continue
        short = str(row.get("indication_short") or "").strip()
        subtype = str(row.get("indication_subtype") or "").strip()
        if subtype:
            official_subtypes = {str(r.get("indication_subtype") or "").strip() for r in official_rows}
            if subtype in official_subtypes:
                logger.info(f"[去重] 网搜分型已被官方档案覆盖: {row.get('generic_name')} / {short}-{subtype}")
                continue
        variants = [short, *DISEASE_VARIANTS.get(short, ())]
        text = "".join(
            str(r.get("indication_full") or "") + str(r.get("indication_short") or "")
            for r in official_rows
        )
        if any(v and v in text for v in variants):
            logger.info(f"[去重] 网搜适应症已被官方档案覆盖: {row.get('generic_name')} / {short}")
            continue
        result.append(row)
    return result


def _full_text_key(value) -> str:
    text = str(value or "")
    text = re.sub(r"^(?:本品)?(?:适用于|用于)?", "", text)
    return re.sub(r"[\s（）()：:，,。.;；·]+", "", text)


def _drop_web_full_text_duplicates(rows: list[dict]) -> list[dict]:
    """同一段适应症全文：有官方档案的行优先，去掉网搜补出来的同文行。

    网搜兜底偶尔会为同一通用名造出品牌/厂家略有出入的“幻影产品”，
    它的适应症全文与官方 PDF 行只差“本品”之类的字面差异。
    """
    official = set()
    for row in rows:
        key = _full_text_key(row.get("indication_full"))
        if len(key) >= 6 and (row.get("nhsa_pdf_url") or row.get("nhsa_ppt_url")):
            official.add(key)
    if not official:
        return rows
    result = []
    for row in rows:
        if row.get("origin_type") == "仿制":
            result.append(row)
            continue
        key = _full_text_key(row.get("indication_full"))
        if (len(key) >= 6 and key in official
                and not (row.get("nhsa_pdf_url") or row.get("nhsa_ppt_url"))):
            continue
        result.append(row)
    return result


def flatten_to_rows(data: dict) -> list:
    rows = []
    generic_name = data.get("generic_name", "")
    for product in data.get("products", []):
        if product.get("approval_status") == "未上市": continue
        
        is_generic = product.get("origin_type") == "仿制"
        has_nhsa_file = bool(product.get("nhsa_pdf_url") or product.get("nhsa_ppt_url"))
        
        local_nhi = _match_nhi(product.get("generic_name", generic_name), product.get("dosage_form", "")) if (not is_generic or has_nhsa_file) else None
        target_nhi = local_nhi
        if is_generic and not has_nhsa_file:
            target_nhi = _match_nhi(product.get("generic_name", generic_name))
            
        nhi_for_base = target_nhi or local_nhi
        p0_ins_year = ""
        if is_generic and nhi_for_base:
            p0_ins_year = nhi_for_base.get("first_execution_date") or nhi_for_base.get("first_year", "")

        inds = [
            ind for ind in product.get("indications", [])
            if _optional_text(ind.get("indication_short")) or _optional_text(ind.get("indication_full"))
        ]
        
        # 死律执行：查字典，找最早进医保的适应症，用药品上市时间给它补齐获批时间
        if target_nhi and product.get("china_approval_date"):
            timeline = target_nhi.get("restriction_timeline", {})
            valid_years = sorted([y for y in timeline.keys() if y.isdigit()], key=int)
            if valid_years:
                earliest_year = valid_years[0]
                earliest_restriction = timeline[earliest_year]
                for ind in inds:
                    ind_name_check = re.sub(r'[（(][^）)]*[）)]', '', ind.get("indication_short", "")).strip()
                    if earliest_restriction == "无限制" or (ind_name_check and ind_name_check in earliest_restriction):
                        if not ind.get("approval_date"):
                            # 方案 A：没有可靠日期就留空，不再用首次上市时间兜底
                            logger.info(
                                f"[时间兜底] {product.get('generic_name')} 适应症缺获批时间，按方案 A 留空待核实。"
                            )
                        break

        display_info = []
        for ind in inds:
            if ind.get("_llm_normalized") and ind.get("indication_short"):
                display_info.append((
                    str(ind.get("indication_short") or "").strip(),
                    str(ind.get("indication_subtype") or "").strip(),
                ))
                continue
            raw_ind_name = re.sub(r'^(降低|存在|疾病进展|风险的)+', '', ind.get("indication_short", "")).strip()
            raw_ind_name = re.sub(r'[（(][^）)]*[）)]', '', raw_ind_name).strip()
            display_info.append(_split_short_subtype(raw_ind_name, ind.get("indication_full", "")))

        processed_inds = []
        for idx, ind in enumerate(inds):
            ind_name, ind_subtype = display_info[idx]
            match_name = ind_subtype or ind_name
            if ind.get("brand_override") and ind.get("brand_name"):
                row_brand = ind["brand_name"]
            else:
                row_brand = _resolve_brand(product.get("brand_name", ""), ind.get("brand_name", ""))
            country = ind.get("approval_country", "中国")
            
            # 医保是中国医保；海外获批适应症不参与中国医保年份匹配
            if country != "中国":
                ind_ins_year = "不适用"
            elif is_generic and not has_nhsa_file:
                ind_ins_year = _derive_insurance_year(target_nhi, match_name, ind.get("approval_date", ""), is_generic=True, p0_ins_year=p0_ins_year) if target_nhi else "未纳入医保"
            else:
                ind_ins_year = _derive_insurance_year(local_nhi, match_name, ind.get("approval_date", ""), is_generic=is_generic, p0_ins_year=p0_ins_year) if local_nhi else "未纳入医保"
            
            processed_inds.append({
                "ind": ind, "ind_name": ind_name, "ind_subtype": ind_subtype,
                "brand_name": row_brand, "country": country, "ind_ins_year": ind_ins_year,
                "_llm_indication": bool(ind.get("_llm_normalized")),
            })

        # 👇 移除了 market_data 组装
        base = {
            "generic_name": product.get("generic_name", generic_name), "drug_type": data.get("drug_type", ""),
            "brand_name": product.get("brand_name", ""), "manufacturer": product.get("manufacturer", ""),
            "origin_type": product.get("origin_type", ""), 
            "china_approval_date": product.get("china_approval_date", ""),
            "specification": product.get("specification", ""), "packaging_spec": product.get("packaging_spec", ""),
            "dosage_form": product.get("dosage_form", ""),
            "price_market": product.get("price_market", ""),
            "price_insurance": _insurance_price_text(nhi_for_base),
            "origin_region": product.get("origin_region", ""),
            "nhsa_pdf_url": product.get("nhsa_pdf_url", ""),
            "nhsa_ppt_url": product.get("nhsa_ppt_url", ""),
            "insurance_category": nhi_for_base.get("insurance_category", "") if nhi_for_base else "",
            "insurance_restriction": nhi_for_base.get("latest_restriction", "") if nhi_for_base else "",
            "drug_insurance_date": ((nhi_for_base.get("first_execution_date") or nhi_for_base.get("first_year", "")) if nhi_for_base else "未纳入医保"),
            "fetch_time": data.get("fetch_time", "")
        }

        for pi in processed_inds:
            ind = pi["ind"]
            ind_name = pi["ind_name"]
            ind_subtype = pi["ind_subtype"]
            row_brand = pi["brand_name"]
            country = pi["country"]
            ind_full = ind.get("indication_full", "")
            if country == "中国" and ind_full and not re.search(r"[\u4e00-\u9fa5]", ind_full):
                continue
            ind_ins_year = pi["ind_ins_year"]
            nhi_for_status = local_nhi or target_nhi
            ind_ins_status = _insurance_status_text(nhi_for_status, ind.get("indication_full", ""), ind_name)
            if country != "中国":
                ind_ins_status = "不适用"
            elif ind_ins_status != "未纳入" and nhi_for_status:
                catalog_type = nhi_for_status.get("catalog_type", "基本医保")
                type_label = "商保创新药" if catalog_type == "商业健康保险创新药" else "基本医保"
                ind_ins_status = f"{ind_ins_status}（{type_label}）"
            
            guidelines = ind.get("guidelines", [])
            g_names = " / ".join([g.get("name", "") for g in guidelines if g.get("name")])
            g_recs = " / ".join([g.get("recommendation", "") for g in guidelines if g.get("recommendation")])
            full_dosage = _optional_text(ind.get("dosage_full") or ind.get("dosage", ""))
            ind_approval = _clamp_indication_approval_date(
                ind.get("approval_date", ""), base.get("china_approval_date", ""), country
            )
            ind_approval_source = str(ind.get("approval_source") or "") if ind_approval else ""

            rows.append({**base,
                "brand_name": row_brand,
                "specification": normalize_specification(ind.get("specification") or base.get("specification", "")),
                "indication_short": ind_name, "indication_subtype": ind_subtype,
                "_llm_indication": pi.get("_llm_indication", False),
                "indication_full": ind.get("indication_full", ind_name),
                "approval_country": country,
                "indication_approval_date": ind_approval,
                "indication_approval_source": ind_approval_source,
                "indication_insurance_date": ind_ins_year, "insurance_status_text": ind_ins_status, "dosage": full_dosage, 
                "dosage_single": ind.get("dosage_single", ""), "frequency": ind.get("frequency", ""), 
                "duration": ind.get("duration", ""), "route": ind.get("route", ""), 
                "guideline_name": g_names, "guideline_recommendation": g_recs
            })
    return _country_dedupe_rows(rows)

def _known_indications(name: str) -> list[dict]:
    """已入库（APPROVED）的适应症，网搜时用来判断“是否已经在库”。"""
    try:
        with SessionLocal() as s:
            rows = s.query(Drug).filter(
                or_(Drug.generic_name.contains(name), Drug.brand_name.contains(name)),
                Drug.status == "APPROVED",
            ).all()
            return [
                {"indication_short": r.indication_short or "", "indication_full": r.indication_full or ""}
                for r in rows
            ]
    except Exception as e:
        logger.warning(f"读取已入库适应症失败: {e}")
        return []


def _cleanup_drafts() -> None:
    try:
        with SessionLocal() as s:
            cutoff = datetime.now() - timedelta(hours=24)
            s.query(Drug).filter(Drug.status == "DRAFT", Drug.updated_at < cutoff).delete(synchronize_session=False)
            s.query(Drug).filter(or_(Drug.status.is_(None), Drug.status.notin_(["DRAFT", "APPROVED"]))).delete(synchronize_session=False)
            s.commit()
    except Exception as e:
        logger.warning(f"清理待审核失败: {e}")


def split_drug_names(raw: str) -> list[str]:
    """空格/逗号/顿号/分号/换行分隔的一大批药名，去重保序。"""
    names = [n.strip() for n in re.split(r"[,，、;；\s]+", str(raw or "")) if n.strip()]
    return list(dict.fromkeys(names))[:QUERY_MAX_BATCH]


@app.get("/query/stream")
async def query_stream(request: Request, drug_names: str):
    _cleanup_drafts()
    names = split_drug_names(drug_names)
    semaphore = asyncio.Semaphore(max(1, QUERY_CONCURRENCY))
    loop, queue = asyncio.get_running_loop(), asyncio.Queue()
    
    def progress_cb(drug, percent, stage): 
        asyncio.run_coroutine_threadsafe(queue.put({"type":"progress","drug":drug,"percent":percent,"stage":stage}), loop)
    
    async def process_drug(name):
        async with semaphore:
            return await _process_one(name)

    async def _process_one(name):
        try:
            known = await loop.run_in_executor(None, _known_indications, name)
            data = await loop.run_in_executor(
                None, lambda: fetch_new_drug(name, lambda p, s: progress_cb(name, p, s), known_indications=known)
            )
            if "error" in data:
                return await queue.put({"type":"biz_error","drug":name,"error":str(data["error"])})

            # 查询结果存 DRAFT，便于补全；每次查询仍然重新抓，不读旧缓存。
            rows = await loop.run_in_executor(None, flatten_to_rows, data)
            for row in rows:
                row["status"] = "DRAFT"
            async with _save_lock:
                saved = await loop.run_in_executor(None, _save_rows, data.get("generic_name", name), rows)
            if not saved:
                logger.warning(f"查询 {name} 未返回任何行（官方材料/网搜均无可用数据）")
            await queue.put({"type":"done","drug":name,"data":saved})
        except Exception as e:
            logger.error(f"查询 {name} 严重报错: {e}", exc_info=True)
            message = str(e)
            if "视觉模型账户欠费" in message:
                message = "视觉模型账户欠费，已停止查询；请充值后再试，未使用网搜兜底"
            await queue.put({"type":"biz_error","drug":name,"error":message})

    async def event_stream():
        tasks = [asyncio.create_task(process_drug(n)) for n in names]
        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, timeout=1.0)
                tasks = list(pending)
                while not queue.empty(): 
                    msg = await queue.get()
                    yield f"event: {msg['type']}\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
        finally:
            for t in tasks: t.cancel()
            yield "event: close\ndata: {}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.get("/stats/cost")
def stats_cost(days: int = 7):
    """近 N 天 LLM/搜索成本汇总（P0）。"""
    return usage_store.cost_summary(days=days)


@app.get("/stats/usage")
def stats_usage(limit: int = 50):
    """最近调用明细，排查用。"""
    return usage_store.recent_usage(limit=limit)


@app.get("/drugs")
def list_drugs(db: Session = Depends(get_db)): 
    return [_drug_to_dict(r) for r in db.query(Drug).filter(Drug.status == "APPROVED").order_by(Drug.updated_at.desc()).all()]


@app.post("/drugs")
def create_drug(payload: dict, db: Session = Depends(get_db)):
    """把前端临时行直接入库；同一身份已存在时转为 APPROVED。"""
    allowed = {c.name for c in Drug.__table__.columns} - {"id", "created_at", "updated_at"}
    data = {k: v for k, v in (payload or {}).items() if k in allowed}
    if not data.get("generic_name"):
        raise HTTPException(status_code=400, detail="缺少通用名")
    data["status"] = "APPROVED"
    row = db.query(Drug).filter(
        Drug.generic_name == data.get("generic_name", ""),
        Drug.brand_name == data.get("brand_name", ""),
        Drug.manufacturer == data.get("manufacturer", ""),
        Drug.dosage_form == data.get("dosage_form", ""),
        Drug.specification == data.get("specification", ""),
        Drug.indication_short == data.get("indication_short", ""),
        Drug.indication_subtype == data.get("indication_subtype", ""),
        Drug.indication_full == data.get("indication_full", ""),
    ).first()
    if row:
        for key, value in data.items():
            setattr(row, key, value)
    else:
        row = Drug(**data)
        db.add(row)
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return _drug_to_dict(row)


@app.post("/drugs/draft")
def create_draft(payload: dict, db: Session = Depends(get_db)):
    allowed = {c.name for c in Drug.__table__.columns} - {"id", "created_at", "updated_at"}
    data = {k: v for k, v in (payload or {}).items() if k in allowed}
    if not data.get("generic_name"):
        raise HTTPException(status_code=400, detail="缺少通用名")
    data["status"] = "DRAFT"
    row = db.query(Drug).filter(
        Drug.generic_name == data.get("generic_name", ""),
        Drug.brand_name == data.get("brand_name", ""),
        Drug.manufacturer == data.get("manufacturer", ""),
        Drug.dosage_form == data.get("dosage_form", ""),
        Drug.specification == data.get("specification", ""),
        Drug.indication_short == data.get("indication_short", ""),
        Drug.indication_subtype == data.get("indication_subtype", ""),
        Drug.indication_full == data.get("indication_full", ""),
    ).first()
    if row:
        if row.status == "APPROVED":
            return _drug_to_dict(row)
        for key, value in data.items():
            setattr(row, key, value)
    else:
        row = Drug(**data)
        db.add(row)
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return _drug_to_dict(row)


@app.patch("/drugs/{drug_id}/draft")
def update_draft(drug_id: int, payload: dict, db: Session = Depends(get_db)):
    row = db.query(Drug).filter(Drug.id == drug_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="药品不存在")
    if row.status == "APPROVED":
        return _drug_to_dict(row)
    allowed = {c.name for c in Drug.__table__.columns} - {"id", "created_at", "updated_at"} - LOCKED_EDIT_FIELDS
    for key, value in (payload or {}).items():
        if key in allowed:
            setattr(row, key, value)
    row.status = "DRAFT"
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return _drug_to_dict(row)


@app.post("/drugs/{drug_id}/approve")
def approve_drug(drug_id: int, db: Session = Depends(get_db)):
    row = db.query(Drug).filter(Drug.id == drug_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="药品不存在")
    row.status = "APPROVED"
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return _drug_to_dict(row)


@app.post("/drugs/approve-all")
def approve_all(payload: dict, db: Session = Depends(get_db)):
    """一键入库：把前端当前表格里的待审核数据批量改为 APPROVED。"""
    raw_ids = (payload or {}).get("ids") or []
    ids = [int(i) for i in raw_ids if str(i).isdigit()][:500]
    if not ids:
        raise HTTPException(status_code=400, detail="缺少 ids")
    rows = db.query(Drug).filter(Drug.id.in_(ids), Drug.status == "DRAFT").all()
    now = datetime.now()
    for row in rows:
        row.status = "APPROVED"
        row.updated_at = now
    db.commit()
    return {"approved": len(rows), "ids": [r.id for r in rows]}


@app.patch("/drugs/{drug_id}")
def update_drug(drug_id: int, payload: dict, db: Session = Depends(get_db)):
    row = db.query(Drug).filter(Drug.id == drug_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="药品不存在")
    was_approved = row.status == "APPROVED"
    allowed = {c.name for c in Drug.__table__.columns} - {"id", "created_at", "updated_at"} - LOCKED_EDIT_FIELDS
    for key, value in (payload or {}).items():
        if key in allowed:
            setattr(row, key, value)
    row.status = "APPROVED"
    if not was_approved:
        row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return _drug_to_dict(row)

def _drug_to_dict(d: Drug) -> dict: 
    result = {}
    for c in d.__table__.columns:
        if c.name == "created_at": continue
        val = getattr(d, c.name)
        if isinstance(val, str):
            val = re.sub(r'[\x00-\x1F\x7F]', '', val).strip()
        if c.name == "updated_at" and val:
            val = val.strftime("%Y-%m-%d %H:%M:%S")
        result[c.name] = val
    return result