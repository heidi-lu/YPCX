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

from db import init_db, get_db, SessionLocal
from models import Drug, Feedback
from agent import fetch_new_drug, update_existing_drug, normalize_manufacturer, _normalize_disease_with_dict

os.environ["ANYIO_BACKEND"] = "asyncio"

logger = logging.getLogger(__name__)
_DISEASE_ALIASES = {}
try:
    if os.path.exists("medical_aliases.json"):
        with open("medical_aliases.json", "r", encoding="utf-8") as _f:
            _raw_aliases = json.load(_f)
        for _canonical, _data in _raw_aliases.items():
            _DISEASE_ALIASES[_canonical] = [str(a) for a in _data.get("aliases", []) if a]
except Exception as _e:
    logger.warning(f"疾病别名加载失败: {_e}")

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
    # 清理：DRAFT 保留 24 小时；旧的 MACHINE/空状态清掉；APPROVED 永久保留
    try:
        with SessionLocal() as s:
            cutoff = datetime.now() - timedelta(hours=24)
            s.query(Drug).filter(Drug.status == "DRAFT", Drug.updated_at < cutoff).delete(synchronize_session=False)
            s.query(Drug).filter(or_(Drug.status.is_(None), Drug.status.notin_(["DRAFT", "APPROVED"]))).delete(synchronize_session=False)
            s.commit()
    except Exception as e:
        logger.warning(f"清理草稿失败: {e}")

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
    for alias in _DISEASE_ALIASES.get(canonical, []):
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


def _derive_insurance_year(nhi_data: dict, ind_short: str, approval_date: str, is_generic: bool = False, p0_ins_year: str = "") -> str:
    if not nhi_data: return "未纳入医保"
    timeline = nhi_data.get("restriction_timeline", {})
    execution_dates = nhi_data.get("execution_dates", {})
    if not isinstance(execution_dates, dict):
        execution_dates = {}
    if not timeline: return "未纳入医保"

    valid_years = sorted([y for y in timeline.keys() if y.isdigit()], key=int)
    if not valid_years: return "未纳入医保"

    app_year = 0
    if approval_date:
        m = re.search(r"(\d{4})", str(approval_date))
        if m: app_year = int(m.group(1))

    terms = _disease_terms(ind_short)
    matched_year = None
    for year in valid_years:
        restriction = re.sub(r"\s+", "", str(timeline.get(year, "")))
        unrestricted = restriction == "无限制"
        explicit = any(_term_in_text(term, restriction) for term in terms)
        if unrestricted:
            # “无限制”只代表目录没写限定，不能给获批时间未知的新适应症硬套年份
            if app_year and int(year) >= app_year:
                matched_year = year
                break
            continue
        if explicit:
            if app_year and int(year) < app_year:
                continue
            matched_year = year
            break

    if not matched_year: return "未纳入医保"

    if is_generic:
        p0_year = 0
        if p0_ins_year and p0_ins_year != "未纳入医保":
            m = re.search(r"(\d{4})", p0_ins_year)
            if m: p0_year = int(m.group(1))
        final_y = max(int(matched_year), app_year, p0_year)
        final_key = str(final_y)
        return execution_dates.get(final_key, final_key) if final_y > 0 else "未纳入医保"
    return execution_dates.get(matched_year, matched_year)


def _save_rows(name: str, rows: list) -> list:
    with SessionLocal() as s:
        seen = set()
        for row in rows:
            for k, v in row.items():
                if isinstance(v, str):
                    v = re.sub(r'[\x00-\x1F\x7F]', '', v).strip()
                    if k == "brand_name" and re.search(r'[^\w\u4e00-\u9fa5®™\-\s]', v): v = ""
                    if k == "manufacturer":
                        v = normalize_manufacturer(v)
                    row[k] = v

            k = (row.get("generic_name",""), row.get("brand_name",""), row.get("manufacturer",""),
                 row.get("dosage_form",""), row.get("specification",""),
                 row.get("indication_short",""), row.get("indication_subtype",""))
            if k in seen: continue
            seen.add(k)
            
            ex = s.query(Drug).filter(
                Drug.generic_name==k[0], Drug.brand_name==k[1], Drug.manufacturer==k[2],
                Drug.dosage_form==k[3], Drug.specification==k[4],
                Drug.indication_short==k[5], Drug.indication_subtype==k[6]
            ).first()
            
            if ex:
                # 已有行只补空字段，绝不用新值覆盖已有值
                for ck, v in row.items():
                    if not hasattr(Drug, ck) or v in (None, ""):
                        continue
                    if getattr(ex, ck) in (None, ""):
                        setattr(ex, ck, v)
            else: 
                safe_row = {ck: cv for ck, cv in row.items() if hasattr(Drug, ck) and cv is not None}
                s.add(Drug(**safe_row))
        s.commit()
        results = s.query(Drug).filter((Drug.generic_name.contains(name)) | (Drug.brand_name.contains(name))).all()
        return [_drug_to_dict(d) for d in results]

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

        inds = product.get("indications", [])
        
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
                            ind["approval_date"] = product.get("china_approval_date")
                            logger.info(f"[时间兜底] {product.get('generic_name')} 最早进医保适应症缺时间，用上市时间兜底。")
                        break

        display_info = []
        for ind in inds:
            raw_ind_name = re.sub(r'^(降低|存在|疾病进展|风险的)+', '', ind.get("indication_short", "")).strip()
            raw_ind_name = re.sub(r'[（(][^）)]*[）)]', '', raw_ind_name).strip()
            display_info.append(_display_indication(raw_ind_name))

        # 同一药品同一大类下有多个细分时，简称直接显示细分；只有一个时显示大类
        subtypes_by_display = {}
        for idx, ind in enumerate(inds):
            display, subtype = display_info[idx]
            if ind.get("approval_country", "中国") == "中国" and subtype:
                subtypes_by_display.setdefault(display, set()).add(subtype)

        processed_inds = []
        for idx, ind in enumerate(inds):
            display, ind_subtype = display_info[idx]
            ind_name = ind_subtype if ind_subtype and len(subtypes_by_display.get(display, set())) > 1 else display
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
                "brand_name": row_brand, "country": country, "ind_ins_year": ind_ins_year
            })

        # 👇 移除了 market_data 组装
        base = {
            "generic_name": product.get("generic_name", generic_name), "drug_type": data.get("drug_type", ""),
            "brand_name": product.get("brand_name", ""), "manufacturer": product.get("manufacturer", ""),
            "origin_type": product.get("origin_type", ""), 
            "china_approval_date": product.get("china_approval_date", ""),
            "specification": product.get("specification", ""), "packaging_spec": product.get("packaging_spec", ""),
            "dosage_form": product.get("dosage_form", ""),
            "price_market": product.get("price_market", ""), "nhsa_pdf_url": product.get("nhsa_pdf_url", ""),
            "nhsa_ppt_url": product.get("nhsa_ppt_url", ""),
            "insurance_category": nhi_for_base.get("insurance_category", "") if nhi_for_base else "",
            "insurance_restriction": nhi_for_base.get("latest_restriction", "") if nhi_for_base else "",
            "insurance_date": (nhi_for_base.get("first_execution_date") or nhi_for_base.get("first_year", "")) if nhi_for_base else "",
            "fetch_time": data.get("fetch_time", "")
        }

        for pi in processed_inds:
            ind = pi["ind"]
            ind_name = pi["ind_name"]
            ind_subtype = pi["ind_subtype"]
            row_brand = pi["brand_name"]
            country = pi["country"]
            ind_ins_year = pi["ind_ins_year"]
            
            guidelines = ind.get("guidelines", [])
            g_names = " / ".join([g.get("name", "") for g in guidelines if g.get("name")])
            g_recs = " / ".join([g.get("recommendation", "") for g in guidelines if g.get("recommendation")])
            full_dosage = ind.get("dosage_full") or ind.get("dosage", "")

            rows.append({**base,
                "brand_name": row_brand,
                "specification": ind.get("specification") or base.get("specification", ""),
                "indication_short": ind_name, "indication_subtype": ind_subtype,
                "indication_full": ind.get("indication_full", ind_name),
                "approval_country": country, "indication_approval_date": ind.get("approval_date", ""),
                "indication_insurance_date": ind_ins_year, "dosage": full_dosage, 
                "dosage_single": ind.get("dosage_single", ""), "frequency": ind.get("frequency", ""), 
                "duration": ind.get("duration", ""), "route": ind.get("route", ""), 
                "guideline_name": g_names, "guideline_recommendation": g_recs
            })
    return rows

@app.get("/query/stream")
async def query_stream(request: Request, drug_names: str):
    names = list(dict.fromkeys([n.strip() for n in drug_names.split(",") if n.strip()]))[:20]
    loop, queue = asyncio.get_running_loop(), asyncio.Queue()
    
    def progress_cb(drug, percent, stage): 
        asyncio.run_coroutine_threadsafe(queue.put({"type":"progress","drug":drug,"percent":percent,"stage":stage}), loop)
    
    async def process_drug(name):
        try:
            data = await loop.run_in_executor(None, lambda: fetch_new_drug(name, lambda p,s: progress_cb(name,p,s)))
            if "error" in data:
                return await queue.put({"type":"biz_error","drug":name,"error":str(data["error"])})

            # 查询结果存 DRAFT，便于补全；每次查询仍然重新抓，不读旧缓存。
            rows = await loop.run_in_executor(None, flatten_to_rows, data)
            for row in rows:
                row["status"] = "DRAFT"
            async with _save_lock:
                saved = await loop.run_in_executor(None, _save_rows, data.get("generic_name", name), rows)
            await queue.put({"type":"done","drug":name,"data":saved})
        except Exception as e:
            logger.error(f"查询 {name} 严重报错: {e}", exc_info=True)
            await queue.put({"type":"biz_error","drug":name,"error":f"系统异常: {e}"})

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
    allowed = {c.name for c in Drug.__table__.columns} - {"id", "created_at", "updated_at"}
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


@app.patch("/drugs/{drug_id}")
def update_drug(drug_id: int, payload: dict, db: Session = Depends(get_db)):
    row = db.query(Drug).filter(Drug.id == drug_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="药品不存在")
    allowed = {c.name for c in Drug.__table__.columns} - {"id", "created_at", "updated_at"}
    for key, value in (payload or {}).items():
        if key in allowed:
            setattr(row, key, value)
    row.status = "APPROVED"
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