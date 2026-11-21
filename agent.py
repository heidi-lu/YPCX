import json, re, time, logging, os
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field
import requests
from openai import OpenAI

from config import MAIN_LLM_API_KEY, MAIN_LLM_BASE_URL, MAIN_LLM_MODEL, redis_client
from scheduler import DrugDataScheduler
from nhsa_crawler import find_drug_files
from deepseek_search import search as ds_search
from skill_vision_parser import extract_medical_pdf_vision

logger = logging.getLogger(__name__)
_client = OpenAI(api_key=MAIN_LLM_API_KEY, base_url=MAIN_LLM_BASE_URL)
scheduler = DrugDataScheduler(redis_client)
FILE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
MAX_PRODUCTS = 20

# ================= 加载本地权威同义词字典 =================
ALIASES_DICT = {}
if os.path.exists("medical_aliases.json"):
    try:
        with open("medical_aliases.json", "r", encoding="utf-8") as f:
            raw_dict = json.load(f)
            for standard_name, data in raw_dict.items():
                ALIASES_DICT[standard_name.lower()] = standard_name
                for alias in data.get("aliases", []):
                    ALIASES_DICT[alias.lower()] = standard_name
        logger.info(f"✅ 医学字典加载成功，共 {len(ALIASES_DICT)} 个同义词映射。")
    except Exception as e:
        logger.error(f"❌ 医学字典加载失败: {e}")

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
    if not date_str: return ""
    m = re.search(r'(\d{4})', str(date_str))
    if not m: return ""
    year = int(m.group(1))
    if year > datetime.now().year or year < 1990: return ""
    return str(date_str)

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
    text = re.sub(r"^\s*\d+\s*[、.．]\s*", "", text).strip()
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

class BrandMatchResult(BaseModel):
    match: bool = False
    evidence: str | None = ""

class ShortIndicationItem(BaseModel):
    index: int = 0
    indication_short: str | None = ""

class ShortIndicationResult(BaseModel):
    items: List[ShortIndicationItem] = Field(default_factory=list)

# ================= LLM 调用封装 =================
def _parse_dosage_fields(dosage_text: str) -> dict:
    if not dosage_text: return {}
    prompt = f"""提取用药信息。输出JSON：{{"dosage_single": "", "frequency": "", "route": "", "duration": ""}} 原文没写填""，严禁输出 null。原文：{dosage_text}"""
    res = _call_llm(prompt, DosageParseResult)
    return res.model_dump() if res else {}

def _call_llm(prompt, schema_cls, timeout=40):
    for att in range(3):
        try:
            resp = _client.chat.completions.create(
                model=MAIN_LLM_MODEL, messages=[{"role": "user", "content": prompt}], 
                response_format={"type": "json_object"}, timeout=timeout
            )
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.choices[0].message.content.strip(), flags=re.MULTILINE)
            
            # 强杀字符串形式的 "null" 和 "None"
            text = text.replace('"null"', '""').replace('"None"', '""')
            
            return schema_cls.model_validate(json.loads(text))
        except Exception as e:
            logger.warning(f"❌ [LLM API fail] 第{att+1}次重试 | 错误: {e}")
            time.sleep(2)
    return None

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
    result = _call_llm(prompt, BrandMatchResult)
    if not result or not result.match:
        logger.info(f"🌐 品牌校验未通过: {generic} / {brand}")
        return False
    return True


def _normalize_short_indications(products: list[dict]) -> None:
    """用完整适应症原文让 LLM 提取纯疾病名，作为适应症简。"""
    for prod in products:
        items = []
        for idx, ind in enumerate(prod.get("indications", []), start=1):
            if str(ind.get("indication_full") or "").strip():
                items.append((idx, ind))
        if not items:
            continue
        lines = "\n".join(
            f"{idx}. 原文：{ind.get('indication_full', '')}" for idx, ind in items[:20]
        )
        prompt = f"""下面是药品的完整适应症原文。请为每条提取一个“纯疾病名”作为适应症简称。
只输出病名，不要包含：本品、联合、单药、用于、适用于、治疗、患者、一线/二线、方案。
【硬规则】除“非小细胞肺癌”外，简称最多 6 个汉字；超过必须继续删减。
必须删除“鳞状、腺癌、细胞癌、结合部、或、及”等细分修饰，保留常用大类病名。
简称优先采用“器官/部位 + 癌”的形式（如食管癌、胃癌），不要把鳞癌、腺癌、细胞癌当成简称。
保留疾病大类，但不要继续上卷：非小细胞肺癌的各亚型统一为“非小细胞肺癌”，不要归成“肺癌”。
输出 JSON：{{"items":[{{"index":1,"indication_short":"纯疾病名"}}]}}
{lines}"""
        result = _call_llm(prompt, ShortIndicationResult)
        if not result:
            continue
        by_index = {item.index: str(item.indication_short or "").strip() for item in result.items}
        for idx, ind in items:
            short = by_index.get(idx, "")
            short = re.sub(r"[（(].*?[）)]", "", short).strip()
            short = re.sub(r"^[0-9]+[.、]\s*", "", short).strip()
            if short and len(short) <= 30 and not any(x in short for x in ("本品", "联合", "单药", "适用于", "用于治疗")):
                ind["indication_short"] = short


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
        result = _call_llm(prompt, BrandFromMetaResult)
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
            result = _call_llm(prompt, DosageTextResult)
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
        result = _call_llm(prompt, BrandByIndicationResult)
        if not result:
            continue

        product_mfr = normalize_manufacturer(prod.get("manufacturer", ""))
        for item in result.indications:
            brand = _clean_brand_name(item.brand_name or "")
            if not brand or not _is_chinese_brand(brand):
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
                    "nhsa_pdf_url": prod.get("nhsa_pdf_url", ""),
                    "nhsa_ppt_url": prod.get("nhsa_ppt_url", ""),
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
def fetch_new_drug(drug_name: str, progress_callback=None) -> dict:
    emit = lambda p, s: progress_callback(p, s) if progress_callback else None
    
    emit(10, "获取医保局文件")
    files_list = scheduler.get_or_fetch("nhsa_pdf", drug_name, lambda dn, fg, ek: find_drug_files(dn)) or []
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
        
        res = extract_medical_pdf_vision(pdf_b, ppt_b) or {}

        # PDF 原文“说明书全部适应症”块优先，保留【n】完整段落
        if res.get("full_indications"):
            old_by_short = {}
            for old in res.get("indications", []):
                key = _normalize_disease_with_dict(old.get("indication_short", ""))
                if key:
                    old_by_short[key] = old
            merged = []
            for full in res["full_indications"]:
                key = _normalize_disease_with_dict(full.get("indication_short", ""))
                old = old_by_short.get(key, {})
                merged.append({
                    **full,
                    "_from_pdf": True,
                    "approval_date": old.get("approval_date", ""),
                    "dosage_full": old.get("dosage_full", ""),
                    "specification": normalize_specification(old.get("specification") or res.get("specification", "")),
                    "guidelines": old.get("guidelines", []),
                })
            res["indications"] = merged
        
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
            if not origin_type:
                origin_type = ""
        
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
                ]
                if not candidates:
                    continue
                # 同名多条时，优先给围术期/新辅助那条，否则给最后一条
                target = next(
                    (
                        ind for ind in candidates
                        if "新辅助" in str(ind.get("indication_full", ""))
                        or "可切除" in str(ind.get("indication_full", ""))
                    ),
                    candidates[-1],
                )
                target["approval_date"] = v
                if res.get("ppt_guidelines"):
                    target["guidelines"] = res["ppt_guidelines"]

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
                    cleaned_inds.append({
                        "indication_short": ppt_short, "indication_full": ppt_short,
                        "approval_country": "中国", "approval_date": ppt_date, "guidelines": []
                    })
                    existing_china_inds.add(ppt_short)

        cleaned_inds = _split_numbered_indications(cleaned_inds)

        prod = {
            "generic_name": final_name, 
            "brand_name": brand, 
            "manufacturer": final_mfr,
            "origin_type": origin_type,
            "china_approval_date": _validate_date(res.get("china_approval_date", "")),
            "dosage_form": dosage_f, 
            "specification": res.get("specification", ""),
            "indications": cleaned_inds if cleaned_inds else [{"indication_short": "", "approval_country": "中国"}],
            "nhsa_pdf_url": latest_pdf["url"] if latest_pdf else "", 
            "nhsa_ppt_url": latest_ppt["url"] if latest_ppt else ""
        }
        all_extracted_products.append(prod)

    _normalize_short_indications(all_extracted_products)

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
        base_obj = _call_llm(prompt_base, BaseInfoResult)
        
        if base_obj and base_obj.products:
            for p in base_obj.products:
                clean_brand = _clean_brand_name(p.get("brand_name", ""))
                if not clean_brand or not _is_chinese_brand(clean_brand):
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
                
                hits_all = ds_search(f"{primary_generic_name} {clean_brand} 说明书 全部适应症 获批时间", 8)
                prompt_all = f"""请从以下资料中，提取【{clean_brand}】的所有核心信息。严格输出JSON：
                {{"china_listing_date": "首次上市时间(YYYY-MM)","indications": [{{"indication_short": "极简病名","indication_full": "完整描述","approval_date": "获批时间(YYYY-MM)","dosage_full": "用法用量"}}]}}
                【死命令】说明书同一段里有多个适应症时，按分号/句号拆成多条，不能合并成一条；有几个就输出几条。
                资料：{hits_all}"""
                res_all = _call_llm(prompt_all, WebPatchResult)
                
                new_inds = []
                web_listing_date = ""
                if res_all:
                    web_listing_date = _validate_date(res_all.china_listing_date)
                    if res_all.indications:
                        for ind in res_all.indications:
                            short = _normalize_disease_with_dict(_clean_disease_name(ind.get("indication_short", "")))
                            if not short: continue
                            new_inds.append({
                                "indication_short": short, "indication_full": ind.get("indication_full", short),
                                "approval_country": "中国", "approval_date": _validate_date(ind.get("approval_date", "")),
                                "dosage_full": ind.get("dosage_full", ""), "guidelines": []
                            })

                if new_inds:
                    for ind in new_inds:
                        existing_china_inds.add(ind["indication_short"])
                        
                        if not ind.get("approval_date"):
                            query = f"{clean_brand} {ind['indication_short']} 获批时间 NMPA"
                            time_hits = ds_search(query, max_results=3)
                            if time_hits:
                                prompt_time = f"""从资料提取【{clean_brand}】针对【{ind['indication_short']}】的获批时间。只输出JSON：{{"date": "YYYY-MM"}}。资料：{time_hits}"""
                                class TimeResult(BaseModel): date: str | None = ""
                                time_res = _call_llm(prompt_time, TimeResult)
                                if time_res and time_res.date:
                                    m = re.search(r'(\d{4})\D{0,3}(\d{1,2})', str(time_res.date))
                                    if m: 
                                        ind["approval_date"] = f"{m.group(1)}-{int(m.group(2)):02d}"

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
                        all_extracted_products.append({
                            "generic_name": primary_generic_name, "brand_name": clean_brand, "manufacturer": final_mfr,
                            "origin_type": "原研" if is_origin_match else "",
                            "china_approval_date": web_listing_date,
                            "dosage_form": "注射剂", "specification": "", "indications": new_inds, "nhsa_pdf_url": "", "nhsa_ppt_url": ""
                        })
                    existing_brands.add(clean_brand)

    # ================= 第三步：仅给原研搜 FDA，且补齐时间 =================
    for prod in all_extracted_products:
        if prod.get("origin_type") == "原研":
            brand = prod["brand_name"] or prod["generic_name"]
            hits_ov = ds_search(f"{brand} FDA获批 适应症 商品名", 4)
            if hits_ov:
                prompt_ov = f"""提取国外获批适应症，并带上该适应症对应的商品名。输出JSON：
{{"overseas_indications": [{{"brand_name": "", "country": "美国", "indication_short": "", "approval_date": ""}}]}}
资料：{hits_ov}"""
                web_ov = _call_llm(prompt_ov, WebPatchResult)
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
                                    ov_time_res = _call_llm(prompt_ov_time, OVTimeResult)
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

    # ================= 第四步：时间兜底 =================
    for prod in all_extracted_products:
        if prod["china_approval_date"]: continue
        dates = [i.get("approval_date") for i in prod["indications"] if i.get("approval_country") == "中国" and i.get("approval_date")]
        if dates:
            dates.sort()
            prod["china_approval_date"] = dates[0]
                
        prod["drug_type"] = "生物药/化学药"

    return {
        "generic_name": drug_name, "drug_type": "生物药/化学药",
        "products": all_extracted_products[:MAX_PRODUCTS],
        "fetch_time": datetime.now().strftime("%Y-%m-%d")
    }

def update_existing_drug(drug_name: str, existing_data: dict, progress_callback=None) -> dict:
    return existing_data