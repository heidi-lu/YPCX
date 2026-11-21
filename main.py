import json
import asyncio
import os
import re
import logging
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime

from db import init_db, get_db, SessionLocal
from models import Drug, Feedback
from agent import fetch_new_drug, update_existing_drug

os.environ["ANYIO_BACKEND"] = "asyncio"

logger = logging.getLogger(__name__)
app = FastAPI(title="药品信息 Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

NHI_DICT, _NHI_INDEX, _NHI_ALIAS, _NHI_ALIAS_NAME = {}, {}, {}, {}
def _load_dict():
    global NHI_DICT, _NHI_INDEX, _NHI_ALIAS, _NHI_ALIAS_NAME
    if os.path.exists("nhi_dictionary.json"):
        with open("nhi_dictionary.json", "r", encoding="utf-8") as f: NHI_DICT = json.load(f)
        for k, value in NHI_DICT.items():
            name = k.split("|")[0] if "|" in k else k
            _NHI_INDEX.setdefault(name, []).append(k)
            form = value.get("dosage_form", "") if isinstance(value, dict) else ""
            for alias in (value.get("aliases", []) if isinstance(value, dict) else []):
                alias_name = str(alias).strip()
                if alias_name:
                    _NHI_ALIAS.setdefault(f"{alias_name}|{form}", k)
                    _NHI_ALIAS_NAME.setdefault(alias_name, []).append(k)
            for alias_key in (value.get("alias_keys", []) if isinstance(value, dict) else []):
                alias_key = str(alias_key).strip()
                if alias_key:
                    _NHI_ALIAS.setdefault(alias_key, k)
_load_dict()
_save_lock = asyncio.Lock()

@app.on_event("startup")
def startup(): init_db()

def _normalize_dosage_form(raw_form: str) -> str:
    if not raw_form: return ""
    if "注射" in raw_form or "针" in raw_form: return "注射剂"
    if "片" in raw_form: return "片剂"
    if "胶囊" in raw_form: return "胶囊"
    if "口服液" in raw_form or "液体" in raw_form: return "口服液体剂"
    if "颗粒" in raw_form: return "颗粒剂"
    if "乳膏" in raw_form or "软膏" in raw_form: return "软膏剂"
    return raw_form.strip()

def _match_nhi(generic_name: str, dosage_form: str = ""):
    if not generic_name or not NHI_DICT: return None
    clean_form = _normalize_dosage_form(dosage_form)
    if clean_form:
        exact_key = f"{generic_name}|{clean_form}"
        if exact_key in NHI_DICT: return NHI_DICT[exact_key]
        alias_key = f"{generic_name}|{_normalize_dosage_form(dosage_form)}"
        if alias_key in _NHI_ALIAS: return NHI_DICT[_NHI_ALIAS[alias_key]]

    # 旧名/别名查询：只在唯一命中时返回，避免一个旧名跨多个剂型被乱配
    alias_keys = _NHI_ALIAS_NAME.get(generic_name, [])
    if len(set(alias_keys)) == 1:
        return NHI_DICT[alias_keys[0]]

    for k, v in NHI_DICT.items():
        if generic_name == k.split("|")[0]: return v

    clean_name = re.sub(r'[（(].*?[）)]', '', generic_name).strip()
    for k, v in NHI_DICT.items():
        dict_name = re.sub(r'[（(].*?[）)]', '', k.split("|")[0]).strip()
        if clean_name in dict_name or dict_name in clean_name: return v
    return None

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
        m = re.search(r'(\d{4})', str(approval_date))
        if m: app_year = int(m.group(1))

    target = re.sub(r'【.*?】', '', ind_short or "").strip()
    target = re.sub(r'[（(][^）)]*[）)]', '', target).strip()
    
    matched_year = None
    for year in valid_years:
        restriction = timeline[year]
        if restriction == "无限制" or (target and target in restriction):
            matched_year = year
            break

    if not matched_year: return "未纳入医保"

    if is_generic:
        p0_year = 0
        if p0_ins_year and p0_ins_year != "未纳入医保":
            m = re.search(r'(\d{4})', p0_ins_year)
            if m: p0_year = int(m.group(1))
        matched_y = int(matched_year)
        final_y = max(matched_y, app_year, p0_year)
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
                    row[k] = v

            k = (row.get("generic_name",""), row.get("brand_name",""), row.get("manufacturer",""), row.get("dosage_form",""), row.get("indication_short",""))
            if k in seen: continue
            seen.add(k)
            
            ex = s.query(Drug).filter(
                Drug.generic_name==k[0], Drug.brand_name==k[1], Drug.manufacturer==k[2], Drug.dosage_form==k[3], Drug.indication_short==k[4]
            ).first()
            
            if ex:
                for ck, v in row.items():
                    if hasattr(Drug, ck) and v not in (None, ""): setattr(ex, ck, v)
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

        processed_inds = []
        for ind in inds:
            ind_name = re.sub(r'^(降低|存在|疾病进展|风险的)+', '', ind.get("indication_short", "")).strip()
            ind_name = re.sub(r'[（(][^）)]*[）)]', '', ind_name).strip()
            country = ind.get("approval_country", "中国")
            
            if is_generic and not has_nhsa_file:
                ind_ins_year = _derive_insurance_year(target_nhi, ind_name, ind.get("approval_date", ""), is_generic=True, p0_ins_year=p0_ins_year) if target_nhi else "未纳入医保"
            else:
                ind_ins_year = _derive_insurance_year(local_nhi, ind_name, ind.get("approval_date", ""), is_generic=is_generic, p0_ins_year=p0_ins_year) if local_nhi else "未纳入医保"
            
            processed_inds.append({
                "ind": ind, "ind_name": ind_name, "country": country, "ind_ins_year": ind_ins_year
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
            country = pi["country"]
            ind_ins_year = pi["ind_ins_year"]
            
            guidelines = ind.get("guidelines", [])
            g_names = " / ".join([g.get("name", "") for g in guidelines if g.get("name")])
            g_recs = " / ".join([g.get("recommendation", "") for g in guidelines if g.get("recommendation")])
            full_dosage = ind.get("dosage_full") or ind.get("dosage", "")

            rows.append({**base, 
                "indication_short": ind_name, "indication_full": ind.get("indication_full", ind_name),
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
            with SessionLocal() as s:
                cached_rows = [_drug_to_dict(r) for r in s.query(Drug).filter((Drug.generic_name.contains(name)) | (Drug.brand_name.contains(name))).all()]
            if cached_rows:
                await queue.put({"type":"done","drug":name,"data":cached_rows})
                return

            data = await loop.run_in_executor(None, lambda: fetch_new_drug(name, lambda p,s: progress_cb(name,p,s)))
            if "error" in data: 
                return await queue.put({"type":"biz_error","drug":name,"error":str(data["error"])})
            
            async with _save_lock: 
                saved = await loop.run_in_executor(None, _save_rows, data.get("generic_name", name), flatten_to_rows(data))
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
    return [_drug_to_dict(r) for r in db.query(Drug).order_by(Drug.updated_at.desc()).all()]

def _drug_to_dict(d: Drug) -> dict: 
    result = {}
    for c in d.__table__.columns:
        if c.name in ("created_at", "updated_at"): continue
        val = getattr(d, c.name)
        if isinstance(val, str):
            val = re.sub(r'[\x00-\x1F\x7F]', '', val).strip()
        result[c.name] = val
    return result