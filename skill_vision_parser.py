import base64, json, re, logging, io
import pymupdf
from json_repair import repair_json
from pptx import Presentation
import llm_client
from config import VISION_MODEL

logger = logging.getLogger(__name__)


class VisionAccountError(RuntimeError):
    """视觉模型账户欠费/不可用；必须停止查询，不能走网搜兜底。"""
MAX_PAGES = 15
DPI = 150
MAX_B64_PER_IMAGE = 2_000_000

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
    try:
        result = llm_client.chat(
            [{"role": "user", "content": content}],
            model=VISION_MODEL,
            stage="vision",
            json_mode=False,
            temperature=0.0,
            max_tokens=4096,
            timeout=180,
            max_attempts=2,
            raise_on_error=True,
        )
    except Exception as e:
        detail = str(e)
        response = getattr(e, "response", None)
        if response is not None:
            detail += " " + getattr(response, "text", "")
        if "Arrearage" in detail or "overdue-payment" in detail or "欠费" in detail:
            raise VisionAccountError("视觉模型账户欠费，请充值后再查询") from e
        raise
    return result.text if result else ""

def _normalize_compat(text: str) -> str:
    import unicodedata
    out = []
    for ch in str(text or ""):
        code = ord(ch)
        if 0x2E80 <= code <= 0x2FDF or 0xF900 <= code <= 0xFAFF:
            out.append(unicodedata.normalize("NFKC", ch))
        else:
            out.append(ch)
    return "".join(out)


def _extract_pdf_key_info(pdf_bytes: bytes) -> dict:
    res = {}
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            full_text = _normalize_compat("\n".join([page.get_text() for page in doc]))
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
                        
            for i, line in enumerate(lines):
                if "企业名称" in line or "上市许可持有人" in line:
                    val = re.sub(r'.*?(?:企业名称|上市许可持有人)\s*[：:]\s*', '', line).strip()
                    val = re.sub(r"^[（(][^）)]*[）)]", "", val).strip()
                    j = i + 1
                    if not val and j < len(lines):
                        val = lines[j].strip()
                        j += 1
                    # 封面名称常被排版折行（“上海复宏瑞霖生物技术有 / 限公司”），继续拼接
                    while (val and j < len(lines)
                           and re.search(r"[\u4e00-\u9fa5]$", val)
                           and not re.search(r"(?:公司|集团|厂|中心|研究院|大学|医院)$", val)):
                        nxt = lines[j].strip()
                        if not nxt or not re.match(r"^[\u4e00-\u9fa5（）()]{2,15}$", nxt):
                            break
                        val += nxt
                        j += 1
                    if val: res["manufacturer"] = re.sub(r'[_ \s]', '', val)
                    break
            
            clean_text = re.sub(r'\s+', '', full_text)
            m_reg = re.search(r'(?:药品)?注册分类[：:]?([\u4e00-\u9fa5a-zA-Z0-9\.]+)', clean_text)
            if m_reg: res["registration_category"] = m_reg.group(1).strip()

            m_brand = re.search(r"(?:^|\n)商品名\s*[：:]?\s*\n?\s*([^\n]{1,20})", full_text)
            if m_brand:
                b = re.sub(r"[®™]", "", m_brand.group(1)).strip()
                b = re.split(r"通用名|商品名|规格|包装|药品", b)[0].strip()
                if 1 < len(b) <= 6 and re.search(r"[\u4e00-\u9fa5]", b):
                    res["brand_name"] = b

            m_launch = re.search(r"(?:该通用名)?全球首个上市国家/地区\s*\n?\s*([^\n]{1,30})", full_text)
            if m_launch:
                res["first_launch_country"] = m_launch.group(1).strip()
            m_no = re.search(r"注册证号/批准文号\s*\n?\s*([^\n]{1,60})", full_text)
            if m_no:
                res["approval_number"] = m_no.group(1).strip()
    except Exception as e:
        logger.warning(f"PDF 本地解析失败: {e}")
    return res

def _shape_texts(shapes):
    """递归取形状文本：普通文本框、组合形状里的文本框、表格单元格都要。"""
    for shape in shapes:
        try:
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    line = " | ".join(c for c in cells if c)
                    if line:
                        yield line
                continue
            if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
                yield from _shape_texts(shape.shapes)
                continue
            if hasattr(shape, "text") and shape.text.strip():
                yield shape.text.strip()
        except Exception:
            continue


def _collect_ppt_text(ppt_bytes: bytes) -> str:
    text = ""
    if ppt_bytes.startswith(b'PK'):
        try:
            prs = Presentation(io.BytesIO(ppt_bytes))
            for slide in prs.slides:
                text += "\n".join(_shape_texts(slide.shapes)) + "\n"
            return text
        except Exception:
            pass
    if ppt_bytes.startswith(b'%PDF'):
        try:
            with pymupdf.open(stream=ppt_bytes, filetype="pdf") as doc:
                return "\n".join([page.get_text() for page in doc])
        except Exception:
            return ""
    try:
        prs = Presentation(io.BytesIO(ppt_bytes))
        for slide in prs.slides:
            text += "\n".join(_shape_texts(slide.shapes)) + "\n"
        return text
    except Exception:
        pass
    try:
        with pymupdf.open(stream=ppt_bytes, filetype="pdf") as doc:
            return "\n".join([page.get_text() for page in doc])
    except Exception:
        return ""


_REFERENCE_HINT = re.compile(r"参照药品|参比|对照药|阳性对照|原研|仿制")


def _extract_brand_from_text(clean_text: str) -> str:
    """从 PPT 文本里取商品名；跳过“参照药品：西妥昔单抗（爱必妥®）”这类对照药。"""
    text = re.sub(r"\s+", "", str(clean_text or ""))
    # 1) 结构化写法：通用名/商品名 菲诺利单抗注射液/安佑平
    m = re.search(
        r"通用名\s*[/／]?\s*商品名\s*[：:]?\s*([^/／]{1,30}?)\s*[/／]\s*([^/／]{1,10}?)(?=英文名|注册规格|规格|商品名|通用名|$)",
        text,
    )
    if m:
        window = text[max(0, m.start() - 10):m.end() + 10]
        cand = m.group(2).strip(" ：:®™")
        if cand and 1 < len(cand) <= 8 and not _REFERENCE_HINT.search(window):
            return cand
    # 2) “商品名：X”
    for m in re.finditer(r"商品名\s*[：:]?\s*([\u4e00-\u9fa5A-Za-z®™]{1,12})", text):
        # “商品名：”本身就说明是本品；只防前面紧挨着“参照药品”的写法
        window = text[max(0, m.start() - 10):m.start()]
        if _REFERENCE_HINT.search(window):
            continue
        cand = re.sub(r"[®™]", "", m.group(1))
        cand = re.split(r"通用名|商品名|规格|包装|英文名|注册", cand)[0].strip()
        if 1 < len(cand) <= 8 and cand not in {"通用名", "商品名"}:
            return cand
    # 3) （X®）——跳过对照药上下文
    for m in re.finditer(r"[（(]([^）)]{1,10}?)[®™][）)]", text):
        window = text[max(0, m.start() - 40):m.end() + 20]
        if _REFERENCE_HINT.search(window):
            continue
        cand = m.group(1).strip()
        if 1 < len(cand) <= 8:
            return cand
    # 4) X®
    for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z]{2,6})[®™]", text):
        window = text[max(0, m.start() - 40):m.end() + 20]
        if _REFERENCE_HINT.search(window):
            continue
        cand = m.group(1).strip()
        if cand not in {"通用名", "商品名"}:
            return cand
    return ""


def _extract_ppt_info(ppt_bytes: bytes) -> dict:
    res = {}
    text = _collect_ppt_text(ppt_bytes)

    if not text.strip(): return {}
    text = _normalize_compat(text)

    clean_text = re.sub(r'\s+', '', text)
    m_reg = re.search(r'(?:药品)?注册分类[：:]?([\u4e00-\u9fa5a-zA-Z0-9\.]+)', clean_text)
    if m_reg: res["registration_category"] = m_reg.group(1).strip()

    brand = _extract_brand_from_text(clean_text)
    if brand:
        res["brand_name"] = brand

    try:
        prompt = f"""请仔细阅读以下 PPT 原文（含表格内容），提取两类信息，严格输出 JSON：
        {{"indication_approval_times": [{{"indication_short": "纯病名", "approval_date": "YYYY-MM"}}],
          "guidelines": [{{"indication_short": "纯病名", "name": "指南/专家共识全名", "recommendation": "推荐内容摘要", "level": "证据或推荐级别"}}]}}
        【死命令】：
        1. 病名只保留纯病名，绝对不能带“拟新增”三个字。
        2. 指南只提取原文明确出现的指南名和推荐内容，不要编造；同一条指南的多个推荐合并成一条。
        3. 找不到的字段返回空列表 []。
        原文：{text[:8000]}"""
        result = llm_client.chat(
            [{"role": "user", "content": prompt}],
            stage="ppt",
            json_mode=True,
            timeout=120,
        )
        if not result:
            return res
        res_json = _clean_parse_json(result.text)
        if res_json.get("indication_approval_times"):
            res["indication_approval_times"] = res_json["indication_approval_times"]
        if res_json.get("guidelines"):
            res["guidelines"] = res_json["guidelines"][:30]
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
            "dosage_full":"用法用量", "guidelines":[{"name":"", "recommendation":"", "level":""}]
        }]}
        【死命令】：1. 遇到分号必须拆分。2. 不要输出获批时间；说明书正文通常没有分适应症获批日期，由 PPT/官方公告另行解析。3. 找不到的字段填 ""。"""
        resp = _call_vlm(urls, prompt)
        return _clean_parse_json(resp)
    except VisionAccountError:
        raise
    except: return {}

def extract_medical_pdf_vision(pdf_bytes: bytes | None, ppt_bytes: bytes | None = None) -> dict:
    res = {"cover_drug_name": "", "manufacturer": "", "registration_category": "", 
           "china_approval_date": "", "dosage_form": "", "specification": "", 
           "indications": [], "brand_name": "", "indication_approval_times": [], "guidelines": []}

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
        if ppt_res.get("guidelines"): res["guidelines"] = ppt_res["guidelines"]
    return res