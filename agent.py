import json, re, time, logging, os
from datetime import datetime, timedelta
from typing import List, Optional
from pydantic import BaseModel, Field
import requests

from config import redis_client
from scheduler import DrugDataScheduler
from nhsa_crawler import find_drug_files
from deepseek_search import search as ds_search
from skill_vision_parser import extract_medical_pdf_vision
from approval_dates import resolve_indication_approval_date
from disease_terms import ABBREVIATIONS, ALIASES_DICT, DISEASE_VARIANTS
from indication_regex import (
    extract_indications_from_pdf,
    disease_short_from_full,
    looks_like_short_fragment,
    strip_short_patient_tail,
)
import llm_client
import source_snapshot
import usage_store

logger = logging.getLogger(__name__)
scheduler = DrugDataScheduler(redis_client)
FILE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
MAX_PRODUCTS = 20

# ================= 加载原研厂家字典 =================
ORIGIN_MFR_MAPPING = {} 
if os.path.exists("origin_manufacturers.json"):
    try:
        with open("origin_manufacturers.json", "r", encoding="utf-8") as f:
            raw_mfrs = json.load(f)
            for standard_name, aliases in raw_mfrs.items():
                ORIGIN_MFR_MAPPING[standard_name.lower()] = standard_name
                for alias in aliases:
                    ORIGIN_MFR_MAPPING[alias.lower()] = standard_name
        logger.info(f"✅ 原研厂家字典加载成功，共 {len(ORIGIN_MFR_MAPPING)} 个别名映射。")
    except Exception as e:
        logger.error(f"❌ 原研厂家字典加载失败: {e}")

def _clean_brand_name(raw: str) -> str:
    if not raw: return ""
    b = re.sub(r'[®™]', '', str(raw)).strip()
    if re.search(r'[^\w\u4e00-\u9fa5\-\s]', b): return ""
    if len(b) % 2 == 0:
        half = len(b) // 2
        if b[:half] == b[half:]:
            b = b[:half]
    return b

PLACEHOLDER_MANUFACTURERS = {
    "上市许可持有人", "上市许可持有人（授权企业）", "企业名称", "授权企业",
    "生产企业", "MAH", "未提及", "未知", "暂无",
}


def normalize_manufacturer(mfr: str) -> str:
    """修复截断的法人后缀，再统一简化为“泰州迈博太科”这种短名。"""
    if not mfr:
        return ""
    original = str(mfr).strip()
    if original in PLACEHOLDER_MANUFACTURERS:
        return ""
    res = re.sub(r'[（(].*?[）)]', '', original).strip()
    # PDF/LLM 常见截断：有限公 / 有限责任公 / 股份有限公
    repairs = [
        ("股份有限公", "股份有限公司"),
        ("有限责任公", "有限责任公司"),
        ("有限公", "有限公司"),
    ]
    for broken, full in repairs:
        if res.endswith(broken):
            res = res[:-len(broken)] + full
            break
    suffixes = ["股份有限公司", "有限责任公司", "有限公司", "集团", "分公司", "制药", "药业"]
    for suffix in suffixes:
        res = res.replace(suffix, "")
    res = res.strip(" ，,;；")
    return res if res else original


def _is_chinese_brand(brand: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fa5]", str(brand or "")))


def normalize_specification(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\)+", ")", text)
    text = re.sub(r"\(+", "(", text)
    return text


def _local_simplify_manufacturer(mfr: str) -> str:
    return normalize_manufacturer(mfr)

def _validate_date(date_str: str) -> str:
    """统一成 YYYY-MM 或 YYYY-MM-DD；非法/未来日期返回空（方案 A：宁可不写）。"""
    if not date_str:
        return ""
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})(?:\D{0,3}(\d{1,2}))?", str(date_str))
    if not m:
        return ""
    year, month = int(m.group(1)), int(m.group(2))
    day = int(m.group(3)) if m.group(3) else 0
    now = datetime.now()
    if not 1990 <= year <= now.year:
        return ""
    if not 1 <= month <= 12:
        return ""
    if day:
        if not 1 <= day <= 31 or (year, month, day) > (now.year, now.month, now.day):
            return ""
        return f"{year:04d}-{month:02d}-{day:02d}"
    if (year, month) > (now.year, now.month):
        return ""
    return f"{year:04d}-{month:02d}"

def _clean_disease_name(raw: str) -> str:
    if not raw: return ""
    cleaned = re.sub(r'【.*?】', '', str(raw)).strip()
    cleaned = re.sub(r'[（(][^）)]*[）)]', '', cleaned)
    cleaned = re.sub(r'^(降低|存在|疾病进展|风险的|用于|治疗|适用于|用于治疗|用于成人|儿童|IgE介导的|拟新增|新增)', '', cleaned).strip()
    cleaned = cleaned.split('。')[0].split('；')[0].split(';')[0].strip()
    return cleaned.strip()

BRACKET_NO = re.compile(r"(?:【|\[)\s*(\d+)\s*(?:】|\])")
LINE_NO = re.compile(r"(?m)^\s*(\d+)\s*[、.．]\s*")


def _split_numbered_sections(text: str) -> list[str]:
    """只按编号拆分；【1】/[1] 优先，行首 1、2、 次之；其它情况原样返回。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    bracket_count = len(BRACKET_NO.findall(raw))
    if bracket_count >= 2:
        parts = re.split(r"(?=(?:【|\[)\s*\d+\s*(?:】|\]))", raw)
        return [p.strip() for p in parts if p.strip()]
    line_count = len(LINE_NO.findall(raw))
    if line_count >= 2:
        parts = re.split(r"(?m)(?=^\s*\d+\s*[、.．]\s*)", raw)
        return [p.strip() for p in parts if p.strip()]
    return [raw]


def _short_from_numbered_section(part: str) -> str:
    text = re.sub(r"^(?:【|\[)\s*\d+\s*(?:】|\])", "", part).strip()
    text = re.sub(r"^\s*(?:[（(]?\d+\s*[)）.、．:：]|[①②③④⑤⑥⑦⑧⑨⑩])", "", text).strip()
    # 取“本品/用于/适用/联合/（”之前的病名
    text = re.split(r"(?:本品|用于|适用|联合|（|\()", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s+", "", text)
    return _clean_disease_name(text)


def _split_numbered_indications(indications: list[dict]) -> list[dict]:
    """保留原有拆分结果；只有当全文里出现多个编号段时才再拆。"""
    result = []
    seen = set()
    for ind in indications:
        full = ind.get("indication_full") or ind.get("indication_short") or ""
        parts = _split_numbered_sections(full)
        if len(parts) <= 1:
            result.append(ind)
            continue
        from_pdf = bool(ind.get("_from_pdf"))
        for part in parts:
            short = _short_from_numbered_section(part)
            if not short:
                continue
            key = (short, str(ind.get("specification", "")))
            if key in seen and not from_pdf:
                continue
            seen.add(key)
            row = dict(ind)
            row["indication_short"] = short
            row["indication_full"] = re.sub(r"\s+", " ", part).strip()
            # 合并全文只有一个获批时间，拆开后无法判断属于哪条：按方案 A 留空，避免逐个串日期
            row["approval_date"] = ""
            result.append(row)
    return result


def _strength_from_text(text: str) -> str:
    """Only accept a strength when the indication text mentions exactly one."""
    values = re.findall(r"\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml|mL|IU|万IU)", str(text or ""))
    values = list(dict.fromkeys(v.replace(" ", "") for v in values))
    return values[0] if len(values) == 1 else ""


def _judge_origin_by_reg(reg_cat: str) -> str:
    """通过注册分类硬明细判断"""
    if not reg_cat: return ""
    clean_reg = str(reg_cat).replace(" ", "")
    matches = re.findall(r'(\d(?:\.\d)?类)', clean_reg)
    
    if not matches:
        if "创新" in clean_reg or "原研" in clean_reg: return "原研"
        if "仿制" in clean_reg or "类似药" in clean_reg: return "仿制"
        return ""
        
    cls = matches[0]
    
    # 治疗用生物制品明细
    if "生物制品" in clean_reg:
        if cls in ["1类", "2类", "2.1类", "2.2类", "2.3类", "2.4类", "3.1类", "3.2类"]: 
            return "原研"
        if cls in ["3.3类", "3类"]: 
            return "仿制"
    # 化学药明细
    else:
        if cls in ["1类", "2类", "2.1类", "2.2类", "2.3类", "3.1类", "5.1类"]: 
            return "原研"
        if cls in ["3类", "4类", "3.3类", "3.4类", "5.2类"]: 
            return "仿制"
        
    return ""

def _normalize_disease_with_dict(raw_name: str) -> str:
    short = _clean_disease_name(raw_name)
    if not short: return ""
    # 别名字典会把两者都归一成“肺癌”；亚型必须保留，否则 SCLC/NSCLC 无法区分
    if "非小细胞肺癌" in short:
        return "非小细胞肺癌"
    if "小细胞肺癌" in short:
        return "小细胞肺癌"
    s_low = short.lower()

    if s_low in ALIASES_DICT: return ALIASES_DICT[s_low]

    best_match_std = None
    max_len = 0
    for alias, standard in ALIASES_DICT.items():
        if re.fullmatch(r'[a-z0-9\-]+', alias):
            if re.search(rf'(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])', s_low):
                return standard
        else:
            if alias in s_low and len(alias) > max_len:
                max_len = len(alias)
                best_match_std = standard

    if best_match_std: return best_match_std
    return short

def _normalize_dosage_form(raw_form: str) -> str:
    if not raw_form: return "注射剂"
    if "注射" in raw_form or "针" in raw_form: return "注射剂"
    if "片" in raw_form: return "片剂"
    if "胶囊" in raw_form: return "胶囊"
    if "口服液" in raw_form or "液体" in raw_form: return "口服液体剂"
    if "颗粒" in raw_form: return "颗粒剂"
    if "植入" in raw_form or str(raw_form).strip() == "装置": return "植入剂"
    return str(raw_form).strip()

# ================= 模型定义（修复 NoneType 报错） =================
class DosageParseResult(BaseModel):
    dosage_single: str | None = ""
    frequency: str | None = ""
    route: str | None = ""
    duration: str | None = ""

class BaseInfoResult(BaseModel):
    brand_name: str | None = ""
    products: List[dict] = Field(default_factory=list)

class WebPatchResult(BaseModel):
    china_listing_date: str | None = ""
    indications: List[dict] = Field(default_factory=list)
    overseas_indications: List[dict] = Field(default_factory=list)

class BrandByIndicationItem(BaseModel):
    indication_short: str | None = ""
    brand_name: str | None = ""
    manufacturer: str | None = ""

class BrandByIndicationResult(BaseModel):
    indications: List[BrandByIndicationItem] = Field(default_factory=list)

class BrandFromMetaResult(BaseModel):
    brand_names: List[str] = Field(default_factory=list)
    manufacturer: str | None = ""
    evidence: str | None = ""

class DosageTextResult(BaseModel):
    dosage_full: str | None = ""

class GenericItem(BaseModel):
    brand_name: str | None = ""
    manufacturer: str | None = ""
    approval_date: str | None = ""

class GenericsResult(BaseModel):
    generics: List[GenericItem] = Field(default_factory=list)

class PriceResult(BaseModel):
    price_market: str | None = ""
    price_date: str | None = ""
    source: str | None = ""

class BrandMatchResult(BaseModel):
    match: bool = False
    evidence: str | None = ""

class ShortIndicationItem(BaseModel):
    index: int = 0
    indication_short: str | None = ""

class ShortIndicationResult(BaseModel):
    items: List[ShortIndicationItem] = Field(default_factory=list)

# ================= LLM 调用封装 =================
def _regex_dosage_fields(text: str) -> dict:
    text = str(text or "")
    route = ""
    m_route = re.search(r"(静脉输注|静脉滴注|静脉给药|皮下注射|肌内注射|口服)", text)
    if m_route:
        route = m_route.group(1)

    dose = ""
    for pattern in (
        r"(?:推荐剂量为|推荐剂量|单次剂量为|每次)\s*([0-9.]+\s*(?:mg|g|μg|ug|ml|mL)(?:\s*/\s*(?:kg|m2|次))?)",
        r"([0-9.]+\s*(?:mg|g|μg|ug|ml|mL)(?:\s*/\s*(?:kg|m2))?)\s*/\s*次",
    ):
        m = re.search(pattern, text)
        if m:
            dose = re.sub(r"\s+", " ", m.group(1)).strip()
            break

    frequency = ""
    m_freq = re.search(
        r"(每\s*\d+\s*周\s*(?:给药)?\s*\d+\s*次|每\s*\d+\s*日\s*\d+\s*次|每日\s*\d+\s*次|每周\s*\d+\s*次|每\s*\d+\s*周一次)",
        text,
    )
    if m_freq:
        frequency = re.sub(r"\s+", "", m_freq.group(1))
        frequency = re.sub(r"每(\d+)周给药(\d+)次", r"每\1周给药\2次", frequency)

    duration = ""
    m_dur = re.search(r"(直至疾病进展[^。；;]*|共给药[^。；;]*|持续用药[^。；;]*)", text)
    if m_dur:
        duration = m_dur.group(1).strip()

    return {"dosage_single": dose, "frequency": frequency, "route": route, "duration": duration}


def _parse_dosage_fields(dosage_text: str) -> dict:
    if not dosage_text:
        return {}
    parsed = _regex_dosage_fields(dosage_text)
    if all(parsed.get(k) for k in ("dosage_single", "frequency", "route")):
        return parsed
    prompt = f"""提取用药信息。输出JSON：{{"dosage_single": "", "frequency": "", "route": "", "duration": ""}}
原文没写就填空字符串，严禁输出 null。
原文：{dosage_text}"""
    res = _call_llm(prompt, DosageParseResult, stage="dosage")
    if res:
        for key, value in res.model_dump().items():
            if not parsed.get(key) and value:
                parsed[key] = value
    return parsed


def _call_llm(prompt, schema_cls, timeout=40, stage="extract"):
    """统一走 llm_client：传输层重试在 client 内，这里只负责 JSON 解析重试。"""
    for att in range(3):
        result = llm_client.chat(
            [{"role": "user", "content": prompt}],
            stage=stage,
            json_mode=True,
            timeout=timeout,
            max_attempts=1,
        )
        if not result:
            continue
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.text.strip(), flags=re.MULTILINE)
        # 强杀字符串形式的 "null" 和 "None"
        text = text.replace('"null"', '""').replace('"None"', '""')
        try:
            return schema_cls.model_validate(json.loads(text))
        except Exception as exc:
            logger.warning(f"❌ [LLM JSON] 第{att + 1}次解析失败 | {exc}")
            time.sleep(1)
    return None

def _is_plausible_brand(brand: str) -> bool:
    text = str(brand or "").strip()
    if not text or len(text) > 6:
        return False
    if text.endswith("单抗") or "注射液" in text or "片" in text:
        return False
    return True


def _brand_matches_generic(generic: str, brand: str) -> bool:
    """二次确认品牌是否真的是这个通用名的商品名。"""
    if not generic or not brand:
        return False
    hits = ds_search(f"{generic} {brand} 商品名 通用名 获批", 3)
    if not hits:
        return False
    prompt = f"""判断资料中的商品名【{brand}】是否明确属于通用名【{generic}】。
只输出 JSON：{{"match": true/false, "evidence": "原文依据"}}
资料没有明确对应关系就输出 false。
资料：{hits}"""
    result = _call_llm(prompt, BrandMatchResult, stage="brand")
    if not result or not result.match:
        logger.info(f"🌐 品牌校验未通过: {generic} / {brand}")
        return False
    return True


def _regex_short_cleanup(products: list[dict]) -> None:
    """只做正则简称清洗，不调用大模型。"""
    for prod in products:
        for ind in prod.get("indications", []):
            full = str(ind.get("indication_full", "")).strip()
            short = str(ind.get("indication_short", "")).strip()
            m = re.match(r"^(?:【\d+】|\[\d+\]|[（(]?\d+\s*[)）.、:：]|[①②③④⑤⑥⑦⑧⑨⑩])\s*([^本品用联（(：:]{2,20})", full)
            if m and re.search(r"(癌|瘤|病|综合征|肌无力)", m.group(1)):
                short = m.group(1).strip()
            short = short.strip("：:，,、；;。 ")
            for prefix in ("复发或转移性", "不可切除或晚期", "不可切除或转移性", "局部晚期或转移性", "晚期", "经典型"):
                if short.startswith(prefix):
                    short = short[len(prefix):]
            # 先替换更长的“非鳞状…”，否则会得到“非非小细胞肺癌”
            short = short.replace("非鳞状非小细胞肺癌", "非小细胞肺癌")
            short = short.replace("鳞状非小细胞肺癌", "非小细胞肺癌")
            short = re.sub(r"^(?:食管)?鳞状细胞癌$", "食管癌", short)
            short = re.sub(r"^胃(?:或|及)胃食管(?:交界处|结合部)腺癌$", "胃癌", short)
            short = re.sub(r"[（(].*?[）)]", "", short).strip()
            short = strip_short_patient_tail(short)
            # 简称只保留病名：逗号后面的“既往接受…/一线治疗”属于限定语
            short = re.split(r"[，,]", short)[0].strip()
            # 只在疑似碎片时重取，避免把本来正确的简称改坏
            if looks_like_short_fragment(short, full):
                repaired = disease_short_from_full(full)
                if repaired:
                    short = repaired
            if short:
                ind["indication_short"] = short


def _fill_missing_approval_dates(products: list[dict], limit: int = 2) -> None:
    """通用补日期：只给“新增/新适应症”或纯网搜产品补，避免每个老适应症都搜一遍。"""
    filled = 0
    for prod in products:
        if filled >= limit:
            break
        generic = str(prod.get("generic_name") or "")
        brand = _clean_brand_name(prod.get("brand_name") or "")
        query_name = f"{generic} {brand}".strip()
        china_date = str(prod.get("china_approval_date") or "")
        web_only = not (prod.get("nhsa_pdf_url") or prod.get("nhsa_ppt_url"))
        for ind in prod.get("indications", []):
            if filled >= limit:
                break
            if ind.get("approval_country", "中国") != "中国" or ind.get("approval_date"):
                continue
            full = str(ind.get("indication_full") or "")
            short = str(ind.get("indication_short") or "")
            if len(short) < 2 or not full:
                continue
            if not web_only and not re.search(r"新增|新适应症", full):
                continue
            got = resolve_indication_approval_date(ds_search, query_name, full, short, china_date)
            if not got:
                continue
            ind["approval_date"] = got["date"]
            ind["approval_source"] = got["source"]
            filled += 1
            logger.info(f"📅 补获批时间: {generic} / {short} -> {got['date']} ({got['source']})")


def _fill_brand_from_metadata(products: list[dict]) -> None:
    """解析后第一步：用通用名+厂家+上市时间找本品商品名；只填空，且只接受唯一品牌。"""
    filled = 0
    for prod in products:
        if filled >= 8:
            break
        if str(prod.get("brand_name", "")).strip():
            continue
        generic = str(prod.get("generic_name", "")).strip()
        mfr = normalize_manufacturer(prod.get("manufacturer", ""))
        if not generic or not mfr:
            continue
        date = str(prod.get("china_approval_date", "") or "")
        query = f"{generic} {mfr} {date} 中文商品名 获批上市".replace("  ", " ").strip()
        hits = ds_search(query, 5)
        if not hits:
            continue
        prompt = f"""从资料中找出这个药品的商品名。严格输出 JSON：
{{"brand_names":["商品名"],"manufacturer":"厂家","evidence":"原文依据"}}
只输出资料中明确出现的中文商品名；不要输出英文商品名；可能对应多个商品名时全部列出；不确定就返回空数组。
通用名：{generic}
厂家：{mfr}
上市时间：{date}
资料：{hits}"""
        result = _call_llm(prompt, BrandFromMetaResult, stage="brand")
        if not result:
            continue
        brands = []
        for raw in result.brand_names or []:
            brand = _clean_brand_name(raw)
            if brand and _is_chinese_brand(brand) and brand not in brands:
                brands.append(brand)
        if len(brands) != 1:
            logger.info(f"🌐 本品商品名不唯一，留空: {generic} {brands}")
            continue
        search_mfr = normalize_manufacturer(result.manufacturer or "")
        if search_mfr and mfr not in search_mfr and search_mfr not in mfr:
            logger.info(f"🌐 本品商品名厂家不匹配，跳过: {generic} {brands[0]} {search_mfr} != {mfr}")
            continue
        if not _brand_matches_generic(generic, brands[0]):
            continue
        prod["brand_name"] = brands[0]
        filled += 1
        logger.info(f"🌐 本品商品名: {generic} / {mfr} -> {brands[0]}")


def _enrich_missing_dosage(products: list[dict]) -> None:
    """只补空 dosage_full；不覆盖已有用法用量。"""
    filled = 0
    for prod in products:
        if filled >= 12:
            break
        generic = str(prod.get("generic_name", "")).strip()
        brand = _clean_brand_name(prod.get("brand_name", ""))
        if not generic:
            continue
        for ind in prod.get("indications", []):
            if filled >= 12:
                break
            if str(ind.get("dosage_full") or ind.get("dosage") or "").strip() not in ("", "未提供", "未提及", "未知", "暂无"):
                continue
            short = str(ind.get("indication_short", "")).strip()
            if not short:
                continue
            hits = ds_search(f"{generic} {brand} {short} 用法用量 说明书", 3)
            if not hits:
                continue
            prompt = f"""只提取下面资料中【{generic}】针对【{short}】的用法用量原文。
严格输出 JSON：{{"dosage_full":"完整用法用量"}}
资料没有明确用法用量就返回空字符串，不要编。
资料：{hits}"""
            result = _call_llm(prompt, DosageTextResult, stage="dosage")
            dosage = str(result.dosage_full or "").strip() if result else ""
            if dosage and dosage not in {"未提供", "未提及", "未知", "暂无"}:
                ind["dosage_full"] = dosage
                filled += 1
                logger.info(f"🌐 补用法用量: {generic} / {short}")


def _enrich_missing_brands(products: list[dict]) -> None:
    """最后一个网搜步骤：只补缺失商品名；厂家不同时新增产品，不污染原行。"""
    existing_keys = {
        (
            str(prod.get("generic_name", "")).strip(),
            str(prod.get("brand_name", "")).strip(),
            normalize_manufacturer(prod.get("manufacturer", "")),
        )
        for prod in products
    }
    existing_brand_keys = {
        (str(prod.get("generic_name", "")).strip(), str(prod.get("brand_name", "")).strip())
        for prod in products
        if str(prod.get("brand_name", "")).strip()
    }
    additions: list[dict] = []
    addition_by_key: dict[tuple, dict] = {}
    enriched = 0

    for prod in list(products):
        if enriched >= 8:
            break
        generic = str(prod.get("generic_name", "")).strip()
        inds = prod.get("indications", [])
        missing = [
            ind for ind in inds
            if not _clean_brand_name(ind.get("brand_name", ""))
        ]
        if not generic or not missing:
            continue

        indication_lines = "\n".join(
            f"- {ind.get('indication_short', '')}" for ind in missing[:12]
        )
        hits = ds_search(f"{generic} 商品名 适应症 厂家 获批 上市", 5)
        if not hits:
            continue
        prompt = f"""你是药品数据核对员。根据资料，为下列通用名的适应症匹配商品名和厂家。
只提取资料中明确出现的对应关系；不确定的留空，不要猜。
注意同一通用名在不同适应症下可能有不同商品名；优先中文商品名。
英文别名：Wegovy=诺和盈，Ozempic=诺和泰，Rybelsus=诺和忻。
通用名：{generic}
适应症：
{indication_lines}
输出 JSON：
{{"indications":[{{"indication_short":"适应症名","brand_name":"商品名","manufacturer":"厂家"}}]}}
资料：{hits}"""
        result = _call_llm(prompt, BrandByIndicationResult, stage="brand")
        if not result:
            continue

        product_mfr = normalize_manufacturer(prod.get("manufacturer", ""))
        for item in result.indications:
            brand = _clean_brand_name(item.brand_name or "")
            if not brand or not _is_chinese_brand(brand) or not _is_plausible_brand(brand):
                continue
            if not _brand_matches_generic(generic, brand):
                continue
            search_mfr = normalize_manufacturer(item.manufacturer or "")
            target = _normalize_disease_with_dict(item.indication_short or "")
            matched_ind = None
            for ind in missing:
                ind_norm = _normalize_disease_with_dict(ind.get("indication_short", ""))
                if target and ind_norm and (target == ind_norm or target in ind_norm or ind_norm in target):
                    matched_ind = ind
                    break
            if matched_ind is None:
                continue

            # PDF/申报材料的企业名称是权威的；厂家不一致先看是不是授权企业
            if product_mfr and search_mfr and search_mfr not in product_mfr and product_mfr not in search_mfr:
                # 原产品没有商品名：优先把商品名补到原企业，不要因为授权企业另造一行
                if not prod.get("brand_name"):
                    matched_ind["brand_name"] = brand
                    logger.info(f"🌐 商品名补到申报企业: {generic} / {brand} / {product_mfr} (忽略搜索厂家 {search_mfr})")
                    continue
                key = (generic, brand, search_mfr)
                new_ind = dict(matched_ind)
                new_ind["brand_name"] = brand
                if key in addition_by_key:
                    addition_by_key[key]["indications"].append(new_ind)
                    continue
                # 同一商品名已经存在：这是厂家/MAH 口径冲突，不要新增一个“仿制”行
                if (generic, brand) in existing_brand_keys:
                    continue
                if key in existing_keys:
                    continue
                new_prod = {
                    "generic_name": generic,
                    "brand_name": brand,
                    "manufacturer": search_mfr,
                    "origin_type": "仿制" if prod.get("origin_type") == "原研" else "",
                    "china_approval_date": prod.get("china_approval_date", ""),
                    "dosage_form": prod.get("dosage_form", ""),
                    "specification": prod.get("specification", ""),
                    "indications": [new_ind],
                    # 网搜新增的品牌不继承官方文件 URL，避免冒充官方产品绕过去重
                    "nhsa_pdf_url": "",
                    "nhsa_ppt_url": "",
                }
                additions.append(new_prod)
                addition_by_key[key] = new_prod
                existing_keys.add(key)
                logger.info(f"🌐 商品名新增产品: {generic} / {brand} / {search_mfr}")
                continue

            # 厂家未知时，不能把商品名硬塞给已有厂家，跳过
            if not search_mfr and product_mfr:
                continue

            matched_ind["brand_name"] = brand
            if search_mfr and not product_mfr:
                prod["manufacturer"] = search_mfr

        brands = {ind.get("brand_name") for ind in inds if ind.get("brand_name")}
        if not prod.get("brand_name") and len(brands) == 1:
            prod["brand_name"] = next(iter(brands))
        enriched += 1
        logger.info(f"🌐 商品名补全: {generic} -> {sorted(brands)}")

    products.extend(additions)


# ================= 主流程 =================
_GUIDE_DISEASE_HINTS = (
    "非小细胞肺癌", "小细胞肺癌", "霍奇金", "尿路上皮", "肝细胞癌", "食管", "鼻咽",
    "胃", "微卫星", "宫颈", "乳腺", "结直肠", "黑色素瘤", "头颈", "卵巢", "胰腺",
    "前列腺", "肾细胞", "甲状腺", "骨髓瘤", "白血病", "肉瘤", "间皮瘤", "胆道",
    "特应性皮炎", "结节性痒疹", "慢性阻塞性肺疾病", "鼻窦炎", "荨麻疹",
    "嗜酸性食管炎", "大疱性类天疱疮", "重症肌无力", "视神经脊髓炎", "IgG4",
    "银屑病", "克罗恩", "溃疡性结肠炎", "哮喘",
)
_GUIDE_LINE_HINTS = ("围术期", "新辅助", "辅助治疗", "一线", "二线", "三线")


def _disease_covered(name: str, rows: list[dict]) -> bool:
    """这个病名是否已经被某条完整适应症覆盖（含常见同义写法）。"""
    name = str(name or "").strip()
    if not name:
        return False
    variants = {name}
    variants.update(DISEASE_VARIANTS.get(name, ()))
    for key in _GUIDE_DISEASE_HINTS:
        if _disease_mentioned(name, key):
            variants.add(key)
    for row in rows:
        text = str(row.get("indication_full") or "") + str(row.get("indication_short") or "")
        if any(v and _disease_mentioned(text, v) for v in variants):
            return True
    return False


def _expand_disease_abbr(text: str) -> str:
    """缩写展开表来自 medical_aliases.json 的 abbreviations 字段，不在代码里另建字典。"""
    for short, full in ABBREVIATIONS.items():
        text = text.replace(short, full)
    return text


def _disease_mentioned(text: str, key: str) -> bool:
    """判断病名是否出现；“小细胞肺癌”不能命中“非小细胞肺癌”里的子串。"""
    text = _expand_disease_abbr(str(text or ""))
    key = _expand_disease_abbr(str(key or ""))
    if not text or not key:
        return False
    if key == "小细胞肺癌":
        return "小细胞肺癌" in text.replace("非小细胞肺癌", "")
    return key in text


def _orig_matches_row(orig: dict, full: str) -> bool:
    short = str(orig.get("indication_short") or "").strip()
    if short and _disease_mentioned(full, short):
        return True
    gtext = " ".join(str(g.get("name", "")) for g in (orig.get("guidelines") or []))
    for key in _GUIDE_DISEASE_HINTS:
        if _disease_mentioned(gtext, key) and _disease_mentioned(full, key):
            return True
    return False


def _pick_guidelines(guidelines, full: str, orig_short: str) -> list[dict]:
    """把一条适应症 blob 里的指南逐条过滤到具体适应症行。

    - 指南名里的病名对不上原文：丢掉
    - 推荐里的病名对不上原文：只保留指南名（推荐清空）
    - 推荐写了治疗线（围术期/新辅助/一线…）而原文没有：推荐清空
    - 指南名和推荐都没有病名：只在这条原始适应症的简称能命中原文时继承
    """
    picked = []
    for guide in guidelines or []:
        name = str(guide.get("name") or "").strip()
        rec = str(guide.get("recommendation") or "").strip()
        if not name and not rec:
            continue
        name_hits = [k for k in _GUIDE_DISEASE_HINTS if _disease_mentioned(name, k)]
        rec_hits = [k for k in _GUIDE_DISEASE_HINTS if _disease_mentioned(rec, k)]
        if name_hits and not any(_disease_mentioned(full, k) for k in name_hits):
            continue
        if rec_hits and not any(_disease_mentioned(full, k) for k in rec_hits):
            if not name_hits:
                continue
            rec = ""
        line_hits = [w for w in _GUIDE_LINE_HINTS if w in rec]
        if line_hits:
            rec = _filter_recommendation(rec, full)
        if not name_hits and not rec_hits and not (orig_short and _disease_mentioned(full, orig_short)):
            continue
        if not name_hits and not rec_hits and not rec:
            rec = ""
        picked.append({"name": name, "recommendation": rec})
    return picked


def _filter_recommendation(rec: str, full: str) -> str:
    """按治疗线拆分推荐，只保留与当前适应症原文匹配的段落。"""
    if not rec:
        return rec
    parts = [p.strip() for p in re.split(r"[；;]", rec) if p.strip()]
    if not parts:
        return rec
    kept = []
    for part in parts:
        line_hits = [w for w in _GUIDE_LINE_HINTS if w in part]
        if not line_hits:
            kept.append(part)
            continue
        if any(w in full for w in line_hits):
            kept.append(part)
            continue
        # 原文写“既往治疗失败”时，允许挂“二线”推荐
        if "二线" in line_hits and "既往" in full:
            kept.append(part)
    return "；".join(kept)


def _merge_guidelines(items: list[dict]) -> list[dict]:
    """同名指南合并成一条，避免表格里出现“同一条指南 / 同一条指南”。"""
    order, by_name = [], {}
    for item in items:
        name = item.get("name", "")
        if name not in by_name:
            by_name[name] = []
            order.append(name)
        rec = item.get("recommendation", "")
        if rec and rec not in by_name[name]:
            by_name[name].append(rec)
    merged = []
    for name in order:
        recs = by_name[name]
        merged.append({"name": name, "recommendation": " / ".join(recs)})
    return merged


def _inherit_pdf_indication_fields(regex_inds: list[dict], original: list[dict]) -> None:
    for row in regex_inds:
        full = str(row.get("indication_full", ""))
        collected = []
        for orig in original or []:
            orig_short = str(orig.get("indication_short") or "").strip()
            collected.extend(_pick_guidelines(orig.get("guidelines"), full, orig_short))
            if _orig_matches_row(orig, full):
                for key in ("dosage_full", "specification"):
                    if orig.get(key) and not row.get(key):
                        row[key] = orig[key]
        merged = _merge_guidelines(collected)
        if merged and not row.get("guidelines"):
            row["guidelines"] = merged


def _attach_ppt_guidelines(ppt_guidelines: list[dict], indications: list[dict]) -> None:
    """把 PPT 抽到的指南按“指南自带的病名”挂到对应适应症上。"""
    for ind in indications:
        if ind.get("guidelines"):
            continue
        full = str(ind.get("indication_full", ""))
        collected = []
        for guide in ppt_guidelines or []:
            guide_short = str(guide.get("indication_short") or "").strip()
            if guide_short and not _disease_mentioned(full, guide_short):
                continue
            collected.extend(_pick_guidelines([guide], full, guide_short))
        merged = _merge_guidelines(collected)
        if merged:
            ind["guidelines"] = merged


def _drop_covered_web_inds(inds: list[dict], existing: list[dict]) -> list[dict]:
    return [ind for ind in inds if not _disease_covered(ind.get("indication_short", ""), existing)]


def _keep_recent_web_inds(inds: list[dict], cutoff: str) -> list[dict]:
    return [ind for ind in inds if _ym(ind.get("approval_date")) and _ym(ind.get("approval_date")) >= cutoff]


def _recent_cutoff() -> str:
    """网搜只补最近 12 个月新获批的适应症。"""
    day = datetime.now() - timedelta(days=365)
    return f"{day.year:04d}-{day.month:02d}"


def _ym(value) -> str:
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})", str(value or ""))
    if not m:
        return ""
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return ""
    return f"{m.group(1)}-{month:02d}"


def _judge_origin_region(first_launch_country: str, approval_number: str, manufacturer: str) -> str:
    """国产/进口判定：优先 PDF 的“全球首个上市国家”，其次批准文号，最后厂家字典。"""
    country = str(first_launch_country or "").strip()
    if country:
        return "国产" if "中国" in country else "进口"
    number = str(approval_number or "").strip()
    if number.startswith("国药准字"):
        return "国产"
    mfr = normalize_manufacturer(manufacturer).lower()
    if mfr:
        for alias in ORIGIN_MFR_MAPPING:
            if alias in mfr:
                return "进口"
    return "国产"


def _enrich_market_price(products: list[dict]) -> None:
    """用兔灵网（药品价格315网）补市场最低销售价；找不到就留空。"""
    for prod in products[:3]:
        if str(prod.get("price_market") or "").strip():
            continue
        generic = str(prod.get("generic_name") or "").strip()
        brand = str(prod.get("brand_name") or "").strip()
        if not generic:
            continue
        query = f"{generic} {brand} 兔灵 315jiage 价格".replace("  ", " ").strip()
        hits = ds_search(query, 5)
        if not hits:
            continue
        prompt = f"""从资料中找【{generic} {brand}】在兔灵网（药品价格315网 315jiage.cn）的最低销售价格。
只输出 JSON：{{"price_market":"如 ￥9477.00/盒（600mg）","price_date":"报价日期 YYYY-MM","source":"来源链接"}}
不要使用医保支付价，不要编造；资料没有明确价格就全部留空字符串。
资料：{hits}"""
        result = _call_llm(prompt, PriceResult, stage="price")
        if not result or not result.price_market:
            continue
        price = str(result.price_market).strip()
        date = str(result.price_date or "").strip()
        prod["price_market"] = f"{price}（兔灵{(' ' + date) if date else ''}）"
        if result.source:
            logger.info(f"🌐 兔灵价格: {generic} {price} {result.source}")


def _add_latest_generics(products: list[dict], generic_name: str, cutoff: str, existing_brands: set) -> None:
    """网搜最近一年新获批的仿制药/生物类似药，作为“仿制”产品补进来。"""
    if not products or not generic_name:
        return
    hits = ds_search(f"{generic_name} 最新获批 仿制药 生物类似药 NMPA {cutoff}", 5)
    if not hits:
        return
    prompt = f"""从资料中提取【{generic_name}】在 {cutoff} 之后新获批的仿制药/生物类似药。
    只输出 JSON：{{"generics":[{{"brand_name":"商品名","manufacturer":"厂家","approval_date":"YYYY-MM"}}]}}
    只输出资料中明确出现的；没有就返回空数组 []。
    资料：{hits}"""
    result = _call_llm(prompt, GenericsResult, stage="web")
    if not result or not result.generics:
        return
    active = [p for p in products if p.get("origin_type") == "原研"] or products
    template = active[0]
    base_inds = [
        {
            "indication_short": i.get("indication_short", ""),
            "indication_full": i.get("indication_full", ""),
            "approval_country": "中国",
            "approval_date": i.get("approval_date", ""),
            "guidelines": i.get("guidelines", []),
        }
        for i in (template.get("indications") or [])
        if i.get("indication_short") and i.get("approval_country", "中国") == "中国"
    ]
    if not base_inds:
        return
    added = 0
    for item in result.generics:
        if added >= 2:
            break
        brand = _clean_brand_name(item.brand_name or "")
        mfr = normalize_manufacturer(item.manufacturer or "")
        date = _validate_date(item.approval_date or "")
        if not brand or not mfr or not date:
            continue
        if _ym(date) < cutoff:
            continue
        if brand in existing_brands or any(brand == p.get("brand_name") for p in products):
            continue
        if not _is_chinese_brand(brand) or not _is_plausible_brand(brand):
            continue
        if not _brand_matches_generic(generic_name, brand):
            continue
        products.append({
            "generic_name": generic_name,
            "brand_name": brand,
            "manufacturer": mfr,
            "origin_type": "仿制",
            "origin_region": _judge_origin_region("", "", mfr),
            "china_approval_date": date,
            "dosage_form": template.get("dosage_form", "注射剂"),
            "specification": "",
            "indications": [dict(i) for i in base_inds],
            "nhsa_pdf_url": "",
            "nhsa_ppt_url": "",
        })
        existing_brands.add(brand)
        added += 1
        logger.info(f"🌐 新增仿制药: {generic_name} / {brand} / {mfr} / {date}")


def _fetch_new_drug_impl(drug_name: str, progress_callback=None, known_indications: list[dict] | None = None) -> dict:
    emit = lambda p, s: progress_callback(p, s) if progress_callback else None
    
    emit(10, "获取医保局文件")
    files_list = scheduler.get_or_fetch("nhsa_pdf", drug_name, lambda dn, fg, ek: find_drug_files(dn)) or []
    source_snapshot.save_files(drug_name, files_list)
    valid_files = [f for f in files_list if "_ts" not in f]
    
    grouped_files = {}
    for f in valid_files:
        raw_name = f.get("context", "").split("：")[0].strip()
        g_name = re.sub(r'^YPS[WN]\d{9}-', '', raw_name).strip()
        if not g_name: g_name = drug_name
        clean_group_name = re.sub(r'[（(].*?[）)]', '', g_name).strip()
        
        if clean_group_name not in grouped_files: 
            grouped_files[clean_group_name] = {"pdf": [], "ppt": []}
        if f.get("type") == "pdf": grouped_files[clean_group_name]["pdf"].append(f)
        elif f.get("type") == "ppt": grouped_files[clean_group_name]["ppt"].append(f)

    all_extracted_products = []
    existing_brands = set()
    existing_china_inds = set()

    # ================= 第一步：官方建档 =================
    for g_name, files in grouped_files.items():
        emit(20, f"解析官方文件: {g_name}")
        latest_pdf = sorted(files["pdf"], key=lambda x: x.get("year", 0), reverse=True)[0] if files["pdf"] else None
        latest_ppt = sorted(files["ppt"], key=lambda x: x.get("year", 0), reverse=True)[0] if files["ppt"] else None
        
        if not latest_pdf and not latest_ppt: continue
        
        try:
            pdf_b = requests.get(latest_pdf["url"], headers=FILE_HEADERS, timeout=20).content if latest_pdf else None
            ppt_b = requests.get(latest_ppt["url"], headers=FILE_HEADERS, timeout=20).content if latest_ppt else None
        except Exception as e:
            logger.error(f"❌ [网络] 下载官方文件失败 ({g_name}): {e}")
            continue
        if pdf_b:
            source_snapshot.save_doc(latest_pdf["url"], pdf_b)
        if ppt_b:
            source_snapshot.save_doc(latest_ppt["url"], ppt_b)
        
        res = extract_medical_pdf_vision(pdf_b, ppt_b) or {}
        if pdf_b:
            regex_inds = extract_indications_from_pdf(pdf_b)
            if regex_inds:
                # PPT 里的指南/共识也当作“原始适应症”来源，交给同一套继承逻辑
                ppt_origins = [
                    {"indication_short": str(g.get("indication_short") or "").strip(), "guidelines": [g]}
                    for g in (res.get("guidelines") or [])
                ]
                _inherit_pdf_indication_fields(regex_inds, (res.get("indications") or []) + ppt_origins)
                res["indications"] = regex_inds

        final_name = res.get("cover_drug_name") or g_name
        for junk in ["企业名称", "企业", "申报", "药品名称"]:
            if junk in final_name: final_name = final_name.split(junk)[0]
        
        brand = _clean_brand_name(res.get("brand_name", ""))
        dosage_f = _normalize_dosage_form(res.get("dosage_form", ""))
        
        # 厂家及原研判定
        raw_mfr = _local_simplify_manufacturer(res.get("manufacturer", ""))
        final_mfr = raw_mfr
        origin_type = ""
        
        # 查原研字典一票否决
        for alias, standard_name in ORIGIN_MFR_MAPPING.items():
            if alias in raw_mfr.lower():
                origin_type = "原研"
                final_mfr = standard_name
                break
                
        # 如果字典没命中，查注册分类
        if not origin_type:
            reg_cat = res.get("registration_category", "")
            origin_type = _judge_origin_by_reg(reg_cat)
            if not origin_type and "中国" in str(res.get("first_launch_country", "")):
                # 全球首个上市国家是中国 → 该通用名是中国首发，按原研
                origin_type = "原研"
            if not origin_type:
                origin_type = ""
        
        origin_region = _judge_origin_region(
            res.get("first_launch_country", ""), res.get("approval_number", ""), final_mfr
        )

        if brand: existing_brands.add(brand)

        if res.get("indication_approval_times"):
            ppt_time_dict = {}
            for item in res["indication_approval_times"]:
                k = _normalize_disease_with_dict(item.get("indication_short", ""))
                v = _validate_date(item.get("approval_date", ""))
                if k and v: ppt_time_dict[k] = v
            
            for k, v in ppt_time_dict.items():
                candidates = [
                    ind for ind in res.get("indications", [])
                    if _normalize_disease_with_dict(ind.get("indication_short", "")) == k
                    and not ind.get("approval_date")
                ]
                if not candidates:
                    continue
                # 同一病名可能对应多条适应症（不同联合方案/线数），PPT 只给病名和时间，
                # 无法判断属于哪一条；按方案 A 宁可留空，也不把时间串到别的行
                if len(candidates) > 1:
                    logger.info(f"⚠️ 获批时间同名多行，跳过避免串行: {k} x{len(candidates)}")
                    continue
                candidates[0]["approval_date"] = v

        cleaned_inds = []
        raw_inds = res.get("indications", [])
        
        if raw_inds:
            seen_keys = set()
            for ind in raw_inds:
                # 👇 防幻觉：强制校准疾病名
                raw_short = ind.get("indication_short", "")
                raw_full = ind.get("indication_full", "")
                if "系统性红斑狼疮" in raw_full or "SLE" in raw_full:
                    if "狼疮肾炎" in raw_short or "LN" in raw_short:
                        raw_short = "系统性红斑狼疮"
                elif "狼疮肾炎" in raw_full or "狼疮性肾炎" in raw_full:
                    if "系统性红斑狼疮" in raw_short:
                        raw_short = "活动性狼疮性肾炎" if "活动性" in raw_full else "狼疮肾炎"

                short = _clean_disease_name(raw_short)
                if not short: continue
                
                target_short = _normalize_disease_with_dict(short)
                spec = str(ind.get("specification") or "").strip()
                if not spec:
                    spec = _strength_from_text(ind.get("indication_full", ""))
                dedupe_key = (target_short, spec)
                from_pdf = bool(ind.get("_from_pdf"))
                if not from_pdf and dedupe_key in seen_keys:
                    for existing in cleaned_inds:
                        if existing["indication_short"] == target_short and existing.get("specification", "") == spec and not existing.get("approval_date"):
                            existing["approval_date"] = _validate_date(ind.get("approval_date", ""))
                            break
                    continue
                
                if not from_pdf:
                    seen_keys.add(dedupe_key)
                existing_china_inds.add(target_short)
                
                dosage_dict = {}
                if ind.get("dosage_full"):
                    dosage_dict = _parse_dosage_fields(ind.get("dosage_full"))

                cleaned_inds.append({
                    "indication_short": target_short,
                    "specification": spec,
                    "indication_full": ind.get("indication_full", target_short),
                    "approval_country": "中国", 
                    "approval_date": _validate_date(ind.get("approval_date", "")),
                    "dosage_full": ind.get("dosage_full", ""), 
                    "guidelines": ind.get("guidelines", []),
                    **dosage_dict
                })

        if raw_inds and res.get("indication_approval_times"):
            for ppt_ind in res["indication_approval_times"]:
                ppt_short = _normalize_disease_with_dict(ppt_ind.get("indication_short", ""))
                ppt_date = _validate_date(ppt_ind.get("approval_date", ""))
                if not ppt_short or not ppt_date: continue
                
                exists_in_final = any(ppt_short == i["indication_short"] for i in cleaned_inds)
                if not exists_in_final:
                    if ppt_short in {"恶性肿瘤", "肿瘤", "癌症", "实体瘤"} or len(ppt_short) > 12:
                        continue
                    # 已有完整全文覆盖同一病名时，不再补一条“全称=简称”的碎片行
                    if _disease_covered(ppt_short, cleaned_inds):
                        continue
                    cleaned_inds.append({
                        "indication_short": ppt_short, "indication_full": ppt_short,
                        "approval_country": "中国", "approval_date": ppt_date, "guidelines": []
                    })
                    existing_china_inds.add(ppt_short)

        cleaned_inds = _split_numbered_indications(cleaned_inds)
        if res.get("guidelines"):
            _attach_ppt_guidelines(res["guidelines"], cleaned_inds)

        prod = {
            "generic_name": final_name, 
            "brand_name": brand, 
            "manufacturer": final_mfr,
            "origin_type": origin_type,
            "origin_region": origin_region,
            "price_market": "",
            "china_approval_date": _validate_date(res.get("china_approval_date", "")),
            "dosage_form": dosage_f, 
            "specification": res.get("specification", ""),
            "indications": cleaned_inds if cleaned_inds else [{"indication_short": "", "approval_country": "中国"}],
            "nhsa_pdf_url": latest_pdf["url"] if latest_pdf else "", 
            "nhsa_ppt_url": latest_ppt["url"] if latest_ppt else ""
        }
        all_extracted_products.append(prod)

    _regex_short_cleanup(all_extracted_products)

    # ================= 第一步半：用通用名+厂家+上市时间补本品商品名 =================
    emit(45, "补本品商品名")
    _fill_brand_from_metadata(all_extracted_products)

    # ================= 第二步：网搜查漏补缺 =================
    emit(50, "全网搜索兜底")
    
    origin_manufacturers = [p["manufacturer"] for p in all_extracted_products if p.get("origin_type") == "原研"]
    primary_origin_mfr = origin_manufacturers[0] if origin_manufacturers else ""
    origin_generic_names = [p["generic_name"] for p in all_extracted_products if p.get("origin_type") == "原研"]
    primary_generic_name = origin_generic_names[0] if origin_generic_names else drug_name

    r1 = ds_search(f"{drug_name} 中文商品名 有哪些品牌 获批上市", 10)
    if r1:
        prompt_base = f"""提取所有中文商品名及厂家。必须列出资料中出现的全部中文商品名，不能只返回一个；英文商品名不要。输出JSON：{{"products":[{{"brand_name":"", "manufacturer":""}}]}}资料：{r1}"""
        base_obj = _call_llm(prompt_base, BaseInfoResult, stage="brand")
        
        if base_obj and base_obj.products:
            for p in base_obj.products:
                clean_brand = _clean_brand_name(p.get("brand_name", ""))
                if not clean_brand or not _is_chinese_brand(clean_brand) or not _is_plausible_brand(clean_brand):
                    continue
                if not _brand_matches_generic(drug_name, clean_brand):
                    continue
                
                raw_mfr = _local_simplify_manufacturer(p.get("manufacturer", "").strip())
                if not raw_mfr or raw_mfr in ["未提及", "未知", "暂无"]:
                    continue
                
                is_origin_match = False
                final_mfr = raw_mfr
                
                for alias, standard_name in ORIGIN_MFR_MAPPING.items():
                    if alias in raw_mfr.lower():
                        is_origin_match = True
                        final_mfr = standard_name
                        break
                
                has_official = any(
                    p.get("nhsa_pdf_url") or p.get("nhsa_ppt_url") for p in all_extracted_products
                )
                cutoff = _recent_cutoff()
                existing_inds = [
                    ind for p in all_extracted_products for ind in (p.get("indications") or [])
                    if str(ind.get("indication_short") or "").strip()
                ]
                existing_inds.extend(known_indications or [])
                existing_hint = "；".join(
                    str(ind.get("indication_short") or "") for ind in existing_inds[:20]
                )
                hits_all = ds_search(
                    f"{primary_generic_name} {clean_brand} 全部适应症 获批时间", 8
                )
                prompt_all = f"""请从以下资料中，提取【{clean_brand}】最近一年（{cutoff} 之后）新获批或新增的适应症。严格输出JSON：
                {{"china_listing_date": "首次上市时间(YYYY-MM)","indications": [{{"indication_short": "极简病名","indication_full": "完整描述","approval_date": "获批时间(YYYY-MM)","dosage_full": "用法用量"}}]}}
                【死命令】
                1. 只输出 {cutoff} 之后新获批/新增的适应症；更早的不要输出。
                2. 下面这些已有适应症不要重复输出：{existing_hint or "无"}
                3. 没有新适应症就返回 "indications": []。
                资料：{hits_all}"""
                res_all = _call_llm(prompt_all, WebPatchResult, stage="web")

                # 第二路：只搜最近一年新获批/新增的适应症
                hits_latest = ds_search(
                    f"{primary_generic_name} {clean_brand} 最新获批适应症 新增适应症 NMPA {cutoff}", 6
                )
                res_latest = _call_llm(f"""只提取【{clean_brand}】在 {cutoff} 之后新获批/新增的适应症。严格输出JSON：
                {{"indications": [{{"indication_short": "极简病名","indication_full": "完整描述","approval_date": "获批时间(YYYY-MM)"}}]}}
                已有适应症不要重复：{existing_hint or "无"}；没有新适应症返回 []。
                资料：{hits_latest}""", WebPatchResult, stage="web") if hits_latest else None

                new_inds = []
                web_listing_date = ""
                if res_all:
                    web_listing_date = _validate_date(res_all.china_listing_date)
                for res_obj in (res_all, res_latest):
                    if not res_obj or not res_obj.indications:
                        continue
                    for ind in res_obj.indications:
                        short = _normalize_disease_with_dict(_clean_disease_name(ind.get("indication_short", "")))
                        if not short: continue
                        new_inds.append({
                            "indication_short": short, "indication_full": ind.get("indication_full", short),
                            "approval_country": "中国", "approval_date": _validate_date(ind.get("approval_date", "")),
                            "dosage_full": ind.get("dosage_full", ""), "guidelines": []
                        })

                # 官方档案已有：只补“官方没覆盖”的新适应症
                if has_official and new_inds:
                    new_inds = _drop_covered_web_inds(new_inds, existing_inds)

                if new_inds:
                    for ind in new_inds:
                        existing_china_inds.add(ind["indication_short"])
                        
                        if not ind.get("approval_date"):
                            query = f"{clean_brand} {ind['indication_short']} 获批时间 NMPA"
                            time_hits = ds_search(query, max_results=3)
                            if time_hits:
                                prompt_time = f"""从资料提取【{clean_brand}】针对【{ind['indication_short']}】的获批时间。只输出JSON：{{"date": "YYYY-MM"}}。资料：{time_hits}"""
                                class TimeResult(BaseModel): date: str | None = ""
                                time_res = _call_llm(prompt_time, TimeResult, stage="web")
                                if time_res and time_res.date:
                                    m = re.search(r'(\d{4})\D{0,3}(\d{1,2})', str(time_res.date))
                                    if m: 
                                        ind["approval_date"] = f"{m.group(1)}-{int(m.group(2)):02d}"
                    if has_official:
                        new_inds = _keep_recent_web_inds(new_inds, cutoff)

                if new_inds:
                    existing_products = [
                        prod for prod in all_extracted_products
                        if _clean_brand_name(prod.get("brand_name", "")) == clean_brand
                    ]
                    if existing_products:
                        # 已有 PDF 来源的产品：PDF 全文优先，不用网搜短适应症补行
                        if not any(prod.get("nhsa_pdf_url") for prod in existing_products):
                            for prod in existing_products:
                                have = {i.get("indication_short", "") for i in prod.get("indications", [])}
                                for ind in new_inds:
                                    if ind["indication_short"] not in have:
                                        prod.setdefault("indications", []).append(dict(ind))
                                if not prod.get("manufacturer"):
                                    prod["manufacturer"] = final_mfr
                                if web_listing_date and not prod.get("china_approval_date"):
                                    prod["china_approval_date"] = web_listing_date
                    else:
                        web_region = (
                            all_extracted_products[0].get("origin_region", "")
                            if all_extracted_products
                            else _judge_origin_region("", "", final_mfr)
                        )
                        web_origin_type = "原研" if is_origin_match else ""
                        if not web_origin_type:
                            web_origin_type = next(
                                (p.get("origin_type", "") for p in all_extracted_products if p.get("origin_type")), ""
                            )
                        all_extracted_products.append({
                            "generic_name": primary_generic_name, "brand_name": clean_brand, "manufacturer": final_mfr,
                            "origin_type": web_origin_type,
                            "origin_region": web_region,
                            "price_market": "",
                            "china_approval_date": web_listing_date,
                            "dosage_form": "注射剂", "specification": "", "indications": new_inds, "nhsa_pdf_url": "", "nhsa_ppt_url": ""
                        })
                    existing_brands.add(clean_brand)

    emit(85, "网搜最新仿制药")
    _add_latest_generics(all_extracted_products, primary_generic_name, _recent_cutoff(), existing_brands)

    # ================= 第三步：仅给原研搜 FDA，且补齐时间 =================
    for prod in all_extracted_products:
        if prod.get("origin_type") == "原研" and prod.get("origin_region") != "国产":
            brand = prod["brand_name"] or prod["generic_name"]
            hits_ov = ds_search(f"{brand} FDA获批 适应症 商品名", 4)
            if hits_ov:
                prompt_ov = f"""提取国外获批适应症，并带上该适应症对应的商品名。输出JSON：
{{"overseas_indications": [{{"brand_name": "", "country": "美国", "indication_short": "", "approval_date": ""}}]}}
资料：{hits_ov}"""
                web_ov = _call_llm(prompt_ov, WebPatchResult, stage="web")
                if web_ov and web_ov.overseas_indications:
                    for ov in web_ov.overseas_indications:
                        ov_short = _normalize_disease_with_dict(ov.get("indication_short", ""))
                        # 海外行允许英文商品名；没有就回退到产品已有品牌
                        ov_brand = _clean_brand_name(ov.get("brand_name") or (prod.get("brand_name") or ""))
                        if not ov_brand:
                            continue
                        if ov_short in existing_china_inds:
                            continue
                        # 海外“过敏性哮喘”不能在中国已有哮喘适应症时额外造细分
                        if "哮喘" in ov_short and any(
                            "哮喘" in i.get("indication_short", "")
                            for i in prod["indications"]
                            if i.get("approval_country", "中国") == "中国"
                        ):
                            continue
                        if not any(ov_short == i["indication_short"] for i in prod["indications"]):
                            ov_date = _validate_date(ov.get("approval_date", ""))
                            
                            if not ov_date:
                                query_ov_time = f"{brand} {ov_short} FDA 获批时间"
                                ov_time_hits = ds_search(query_ov_time, max_results=3)
                                if ov_time_hits:
                                    prompt_ov_time = f"""从资料提取【{brand}】针对【{ov_short}】的 FDA 获批时间。只输出JSON：{{"date": "YYYY-MM"}}。资料：{ov_time_hits}"""
                                    class OVTimeResult(BaseModel): date: str | None = ""
                                    ov_time_res = _call_llm(prompt_ov_time, OVTimeResult, stage="web")
                                    if ov_time_res and ov_time_res.date:
                                        m = re.search(r'(\d{4})\D{0,3}(\d{1,2})', str(ov_time_res.date))
                                        if m: ov_date = f"{m.group(1)}-{int(m.group(2)):02d}"
                            
                            prod["indications"].append({
                                "indication_short": ov_short, "indication_full": ov_short,
                                "brand_name": ov_brand,
                                "approval_country": ov.get("country", "美国"), "approval_date": ov_date,
                                "dosage_full": "", "guidelines": []
                            })

    # ================= 第三步半：商品名缺失补全 =================
    emit(88, "网搜补全商品名")
    _enrich_missing_brands(all_extracted_products)

    # ================= 第三步半之后：补缺失用法用量 =================
    emit(90, "补用法用量")
    _enrich_missing_dosage(all_extracted_products)

    emit(93, "补价格")
    _enrich_market_price(all_extracted_products)

    # ================= 第四步：时间兜底 =================
    for prod in all_extracted_products:
        if prod["china_approval_date"]: continue
        dates = [i.get("approval_date") for i in prod["indications"] if i.get("approval_country") == "中国" and i.get("approval_date")]
        if dates:
            dates.sort()
            prod["china_approval_date"] = dates[0]
                
        prod["drug_type"] = "生物药/化学药"

    _fill_missing_approval_dates(all_extracted_products)

    return {
        "generic_name": drug_name, "drug_type": "生物药/化学药",
        "products": all_extracted_products[:MAX_PRODUCTS],
        "fetch_time": datetime.now().strftime("%Y-%m-%d")
    }

def fetch_new_drug(drug_name: str, progress_callback=None, known_indications: list[dict] | None = None) -> dict:
    """对外入口：标记当前药品，保证搜索/LLM 用量能归到药名下。"""
    token = usage_store.set_drug(drug_name)
    try:
        return _fetch_new_drug_impl(drug_name, progress_callback, known_indications)
    finally:
        usage_store.reset_drug(token)


def update_existing_drug(drug_name: str, existing_data: dict, progress_callback=None) -> dict:
    return existing_data