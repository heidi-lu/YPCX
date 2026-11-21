import base64, json, re, threading, logging, io
import pymupdf
from openai import OpenAI
from pydantic import BaseModel, Field
from json_repair import repair_json
from pptx import Presentation
from config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL, MAIN_LLM_API_KEY, MAIN_LLM_BASE_URL, MAIN_LLM_MODEL

logger = logging.getLogger(__name__)
MAX_PAGES = 15
DPI = 150
MAX_B64_PER_IMAGE = 2_000_000
_client_lock = threading.Lock()
_client: OpenAI | None = None

def _get_client() -> OpenAI:
    global _client
    if _client: return _client
    with _client_lock:
        _client = OpenAI(api_key=VISION_API_KEY, base_url=VISION_BASE_URL)
    return _client

def _get_text_client() -> OpenAI:
    return OpenAI(api_key=MAIN_LLM_API_KEY, base_url=MAIN_LLM_BASE_URL)

def _render_pdf_to_data_urls(pdf_bytes: bytes, pages: list[int] = None) -> list[str]:
    urls = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        indices = pages if pages is not None else range(min(doc.page_count, MAX_PAGES))
        for idx in indices:
            if 0 <= idx < doc.page_count:
                pix = doc.load_page(idx).get_pixmap(matrix=pymupdf.Matrix(DPI/72, DPI/72), alpha=False, colorspace=pymupdf.csRGB)
                b64 = base64.standard_b64encode(pix.tobytes("png")).decode("ascii")
                if len(b64) <= MAX_B64_PER_IMAGE: urls.append(f"data:image/png;base64,{b64}")
    return urls

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
def _clean_parse_json(raw: str) -> dict:
    text = _FENCE_RE.sub("", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    try: return json.loads(text[start:end + 1])
    except: return json.loads(repair_json(text[start:end + 1]))

def _call_vlm(image_urls: list[str], prompt: str) -> str:
    content = [{"type": "image_url", "image_url": {"url": u}} for u in image_urls if u.startswith("data:image")]
    content.append({"type": "text", "text": prompt})
    resp = _get_client().chat.completions.create(
        model=VISION_MODEL, messages=[{"role": "user", "content": content}],
        temperature=0.0, max_tokens=4096,
    )
    return resp.choices[0].message.content

def _extract_full_indications_from_text(full_text: str) -> list[dict]:
    """从申报材料原文的“说明书全部适应症/功能主治”块按【n】拆出完整段落。"""
    text = str(full_text or "").replace("\u3000", " ")
    match = re.search(
        r"说明书全部适应症(?:/功能主治)?\s*(.*?)(?:现行医保目录|说明书用法用量|所治疗疾病)",
        text,
        re.S,
    )
    if not match:
        return []
    block = re.sub(r"\s+", "", match.group(1))
    result = []
    parts = []
    if len(re.findall(r"【\d+】", block)) >= 2:
        parts = [p.strip() for p in re.split(r"(?=【\d+】)", block) if re.match(r"【\d+】", p.strip())]
        marker = re.compile(r"^【\d+】")
    else:
        # 兼容 1. 2. 3. ... 这种连续编号，必须从 1 开始且连续
        matches = list(re.finditer(r"(?<!\d)(\d{1,2})\s*[.、]\s*", block))
        # 正文里可能插入“1. 2. 3.”的医保支付范围子列表，只取从 1 开始连续递增的主编号
        selected = []
        expected = 1
        for match in matches:
            if int(match.group(1)) == expected:
                selected.append(match)
                expected += 1
        if len(selected) >= 2:
            for match in selected:
                raw_index = matches.index(match)
                end = matches[raw_index + 1].start() if raw_index + 1 < len(matches) else len(block)
                parts.append(block[match.start():end].strip())
        marker = re.compile(r"^\d{1,2}\s*[.、]\s*")

    for part in parts:
        body = marker.sub("", part).strip()
        short = re.split(r"本品|用于|适用|联合|（|\(", body, maxsplit=1)[0].strip()
        short = re.sub(r"\s+", "", short)
        if not short:
            # 1.2.3. 这种没有病名标题的条目：至少保留可展示的短名，不丢条目
            fallback = re.sub(r"^本品(?:适用于|用于)?", "", body).strip()
            short = fallback[:60]
        if not short:
            continue
        result.append({
            "indication_short": short,
            "indication_full": part,
            "approval_country": "中国",
        })
    return result


def _extract_pdf_key_info(pdf_bytes: bytes) -> dict:
    res = {}
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            full_text = "\n".join([page.get_text() for page in doc])
            idx = -1
            if "中国大陆首次上市时间" in full_text:
                idx = full_text.find("中国大陆首次上市时间") + len("中国大陆首次上市时间")
            elif "首次上市时间" in full_text:
                idx = full_text.find("首次上市时间") + len("首次上市时间")
                
            if idx != -1:
                snippet = full_text[idx:idx+30]
                m = re.search(r'(\d{4})\D{0,3}(\d{1,2})', snippet)
                if m: res["china_approval_date"] = f"{m.group(1)}-{int(m.group(2)):02d}"
            
            lines = full_text.split('\n')
            for i, line in enumerate(lines):
                if "药品名称" in line:
                    val = re.sub(r'.*?药品名称\s*[：:]\s*', '', line).strip()
                    if not val and i+1 < len(lines): val = lines[i+1].strip()
                    if val: res["cover_drug_name"] = re.sub(r'[_ \s]', '', val)
                    break
                        
            # 优先取“企业名称”，不要取“授权企业”；企业名可能跨两行
            m_ent = re.search(
                r"企业名称\s*[：:]\s*(.{0,100}?(?:有限公司|有限责任公司|股份有限公司|集团|分公司))",
                full_text,
                re.S,
            )
            if m_ent:
                res["manufacturer"] = re.sub(r"[_ \s]", "", m_ent.group(1))
            else:
                for i, line in enumerate(lines):
                    if "上市许可持有人" in line:
                        matched = re.search(r'上市许可持有人(?:（授权企业）)?\s*[：:]\s*(.*)', line)
                        val = matched.group(1).strip() if matched else ""
                        if not val and i + 1 < len(lines):
                            val = lines[i + 1].strip()
                        val = re.sub(r'[_ \s]', '', val)
                        if val and "持有人" not in val and "授权企业" not in val:
                            res["manufacturer"] = val
                        break
            
            clean_text = re.sub(r'\s+', '', full_text)
            m_reg = re.search(r'(?:药品)?注册分类[：:]?([\u4e00-\u9fa5a-zA-Z0-9\.]+)', clean_text)
            if m_reg: res["registration_category"] = m_reg.group(1).strip()
            res["full_indications"] = _extract_full_indications_from_text(full_text)
            m_spec = re.search(
                r"说明书全部注册规格\s*(.*?)(?:上市许可持有人|说明书全部适应症)",
                full_text,
                re.S,
            )
            if m_spec:
                res["specification"] = re.sub(r"\s+", "", m_spec.group(1))
    except Exception as e:
        logger.warning(f"PDF 本地解析失败: {e}")
    return res

def _extract_ppt_info(ppt_bytes: bytes) -> dict:
    res = {}
    text = ""
    if ppt_bytes.startswith(b'PK'):
        try:
            prs = Presentation(io.BytesIO(ppt_bytes))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"): text += shape.text + "\n"
        except: pass
    elif ppt_bytes.startswith(b'%PDF'):
        try:
            with pymupdf.open(stream=ppt_bytes, filetype="pdf") as doc:
                text = "\n".join([page.get_text() for page in doc])
        except: pass
    else:
        try:
            prs = Presentation(io.BytesIO(ppt_bytes))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"): text += shape.text + "\n"
        except:
            try:
                with pymupdf.open(stream=ppt_bytes, filetype="pdf") as doc:
                    text = "\n".join([page.get_text() for page in doc])
            except: pass
            
    if not text.strip(): return {}

    clean_text = re.sub(r'\s+', '', text)
    m_reg = re.search(r'(?:药品)?注册分类[：:]?([\u4e00-\u9fa5a-zA-Z0-9\.]+)', clean_text)
    if m_reg: res["registration_category"] = m_reg.group(1).strip()

    m1 = re.search(r'商品名[：:]?([\u4e00-\u9fa5a-zA-Z®™]+)', clean_text)
    if m1:
        b = re.sub(r'[®™]', '', m1.group(1))
        if len(b) <= 6: res["brand_name"] = b
    if not res.get("brand_name"):
        m2 = re.search(r'[（\(]([^）\)]+?)[®™][）\)]', clean_text)
        if m2: res["brand_name"] = m2.group(1).strip()
    if not res.get("brand_name"):
        m3 = re.search(r'([\u4e00-\u9fa5a-zA-Z]{2,6})[®™]', clean_text)
        if m3: res["brand_name"] = m3.group(1).strip()

    try:
        prompt = f"""请仔细阅读以下 PPT 原文。你的核心任务是找到【适应症获批时间】。
        请严格输出JSON：
        {{"indication_approval_times": [{{"indication_short": "纯病名", "approval_date": "YYYY-MM"}}]}}
        【死命令】：
        1. 提取的病名只保留纯病名，绝对不能带“拟新增”三个字。找不到返回空列表 []。
        原文：{text[:4000]}"""
        resp = _get_text_client().chat.completions.create(
            model=MAIN_LLM_MODEL, messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"}
        )
        res_json = _clean_parse_json(resp.choices[0].message.content)
        if res_json.get("indication_approval_times"):
            res["indication_approval_times"] = res_json["indication_approval_times"]
    except Exception as e:
        logger.warning(f"PPT 大模型抽取失败: {e}")
    return res

def _extract_body_info(pdf_bytes: bytes) -> dict:
    try:
        urls = _render_pdf_to_data_urls(pdf_bytes)
        if not urls: return {}
        prompt = """提取正文信息。严格输出JSON：
        {"dosage_form":"", "specification":"", "indications":[{
            "indication_short":"极简疾病名", "indication_full":"完整原话", 
            "specification":"该适应症对应规格(如10.8mg)，没写填''",
            "approval_date":"获批时间(YYYY-MM)，没写填''",
            "dosage_full":"用法用量", "guidelines":[{"name":"", "recommendation":"", "level":""}]
        }]}
        【死命令】：1. 遇到分号必须拆分。2. 找不到的字段填 ""。"""
        resp = _call_vlm(urls, prompt)
        return _clean_parse_json(resp)
    except: return {}

def extract_medical_pdf_vision(pdf_bytes: bytes | None, ppt_bytes: bytes | None = None) -> dict:
    res = {"cover_drug_name": "", "manufacturer": "", "registration_category": "", 
           "china_approval_date": "", "dosage_form": "", "specification": "", 
           "indications": [], "full_indications": [], "brand_name": "", "indication_approval_times": []}

    if pdf_bytes:
        res.update(_extract_pdf_key_info(pdf_bytes))
        body = _extract_body_info(pdf_bytes)
        if body:
            if body.get("dosage_form"): res["dosage_form"] = body["dosage_form"]
            if body.get("specification"): res["specification"] = body["specification"]
            if body.get("indications"): res["indications"] = body["indications"]

    if ppt_bytes:
        ppt_res = _extract_ppt_info(ppt_bytes)
        if ppt_res.get("brand_name") and not res.get("brand_name"): res["brand_name"] = ppt_res["brand_name"]
        if ppt_res.get("registration_category") and not res.get("registration_category"): res["registration_category"] = ppt_res["registration_category"]
        if ppt_res.get("indication_approval_times"): res["indication_approval_times"] = ppt_res["indication_approval_times"]
    return res