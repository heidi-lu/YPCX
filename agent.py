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

def normalize_manufacturer(mfr: str) -> str:
    """修复截断的法人后缀，再统一简化为“泰州迈博太科”这种短名。"""
    if not mfr:
        return ""
    original = str(mfr).strip()
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
            if not brand:
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

            # 资料厂家与现有厂家对不上：说明大概率是另一个商品/厂家，新增行，不改原行
            if product_mfr and search_mfr and search_mfr not in product_mfr and product_mfr not in search_mfr:
                key = (generic, brand, search_mfr)
                new_ind = dict(matched_ind)
                new_ind["brand_name"] = brand
                if key in addition_by_key:
                    addition_by_key[key]["indications"].append(new_ind)
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
            
            for ind in res.get("indications", []):
                ind_short = _normalize_disease_with_dict(ind.get("indication_short", ""))
                for k, v in ppt_time_dict.items():
                    if k == ind_short:
                        ind["approval_date"] = v
                        break

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
                if dedupe_key in seen_keys:
                    for existing in cleaned_inds:
                        if existing["indication_short"] == target_short and existing.get("specification", "") == spec and not existing.get("approval_date"):
                            existing["approval_date"] = _validate_date(ind.get("approval_date", ""))
                            break
                    continue
                
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

    # ================= 第二步：网搜查漏补缺 =================
    emit(50, "全网搜索兜底")
    
    origin_manufacturers = [p["manufacturer"] for p in all_extracted_products if p.get("origin_type") == "原研"]
    primary_origin_mfr = origin_manufacturers[0] if origin_manufacturers else ""
    origin_generic_names = [p["generic_name"] for p in all_extracted_products if p.get("origin_type") == "原研"]
    primary_generic_name = origin_generic_names[0] if origin_generic_names else drug_name

    r1 = ds_search(f"{drug_name} 有哪些商品名 获批上市", 5)
    if r1:
        prompt_base = f"""提取商品名及厂家。输出JSON：{{"products":[{{"brand_name":"", "manufacturer":""}}]}}资料：{r1}"""
        base_obj = _call_llm(prompt_base, BaseInfoResult)
        
        if base_obj and base_obj.products:
            for p in base_obj.products:
                clean_brand = _clean_brand_name(p.get("brand_name", ""))
                
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
                
                if not clean_brand or clean_brand in existing_brands: 
                    continue

                hits_all = ds_search(f"{primary_generic_name} {clean_brand} 获批上市 适应症 获批时间 (site:bydrug.pharmcube.com OR site:cnpharm.com)", 5)
                prompt_all = f"""请从以下资料中，提取【{clean_brand}】的所有核心信息。严格输出JSON：
                {{"china_listing_date": "首次上市时间(YYYY-MM)","indications": [{{"indication_short": "极简病名","indication_full": "完整描述","approval_date": "获批时间(YYYY-MM)","dosage_full": "用法用量"}}]}}
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
            hits_ov = ds_search(f"{brand} FDA获批 适应症", 4)
            if hits_ov:
                prompt_ov = f"""提取国外获批适应症。输出JSON：{{"overseas_indications": [{{"country": "美国", "indication_short": "", "approval_date": ""}}]}}资料：{hits_ov}"""
                web_ov = _call_llm(prompt_ov, WebPatchResult)
                if web_ov and web_ov.overseas_indications:
                    for ov in web_ov.overseas_indications:
                        ov_short = _normalize_disease_with_dict(ov.get("indication_short", ""))
                        if ov_short in existing_china_inds: 
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
                                "approval_country": ov.get("country", "美国"), "approval_date": ov_date,
                                "dosage_full": "", "guidelines": []
                            })

    # ================= 第三步半：商品名缺失补全 =================
    emit(88, "网搜补全商品名")
    _enrich_missing_brands(all_extracted_products)

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