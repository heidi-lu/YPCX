"""从医保局 PDF 的“说明书全部适应症”块按编号正则拆分；不调用大模型。"""
import re
import pymupdf


def _split_numbered(block: str) -> list[str]:
    block = re.sub(r"\s+", "", block)
    block = re.sub(r"1\.20\d{2}年12月31日协议到期.*?其他药品。", "", block, flags=re.S)
    if len(re.findall(r"【\d+】", block)) >= 2:
        return [p.strip() for p in re.split(r"(?=【\d+】)", block) if re.match(r"【\d+】", p.strip())]
    if len(re.findall(r"【[^】]+】", block)) >= 2:
        return [p.strip() for p in re.split(r"(?=【[^】]+】)", block) if re.match(r"【[^】]+】", p.strip())]

    matches = list(re.finditer(r"(?<!\d)(\d{1,2})\s*[.、]\s*", block))
    selected = []
    expected = 1
    for match in matches:
        if int(match.group(1)) == expected:
            selected.append(match)
            expected += 1
    if len(selected) < 2:
        return []
    parts = []
    for match in selected:
        idx = matches.index(match)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(block)
        parts.append(block[match.start():end].strip())
    return parts


def _short(part: str) -> str:
    m_heading = re.match(r"^【([^】]+)】", part)
    if m_heading and not m_heading.group(1).isdigit():
        return re.sub(r"\s+", "", m_heading.group(1))
    body = re.sub(r"^(?:【\d+】|\d{1,2}\s*[.、])", "", part).strip()
    short = re.split(r"本品|用于|适用|联合|（|\(", body, maxsplit=1)[0].strip()
    if not short:
        candidates = re.findall(r"(?<=[，。；的性])([\u4e00-\u9fa5]{2,20}(?:癌|淋巴瘤|白血病|肉瘤|病))", body)
        if not candidates:
            candidates = re.findall(r"([\u4e00-\u9fa5]{2,12}(?:癌|淋巴瘤|白血病|肉瘤))", body)
        if candidates:
            short = candidates[-1]
    short = re.sub(r"\s+", "", short)
    if "非小细胞肺癌" in body:
        short = "非小细胞肺癌"
    elif "小细胞肺癌" in body:
        short = "小细胞肺癌"
    elif "胃或胃食管" in body or "胃及胃食管" in body:
        short = "胃癌"
    elif "食管" in short and "癌" in short:
        short = "食管癌"
    elif "胃" in short and "癌" in short:
        short = "胃癌"
    elif "微卫星" in body or "MSI" in body:
        short = "实体瘤"
    for prefix in ("复发或难治性", "局部晚期或转移性", "晚期", "经典型"):
        if short.startswith(prefix):
            short = short[len(prefix):]
    return short


def extract_indications_from_pdf(pdf_bytes: bytes) -> list[dict]:
    try:
        text = "\n".join(p.get_text() for p in pymupdf.open(stream=pdf_bytes, filetype="pdf"))
        m = re.search(
            r"说明书全部适应症(?:/功能主治)?\s*(.*?)(?:现行医保目录|说明书用法用量|所治疗疾病)",
            text,
            re.S,
        )
        if not m:
            return []
        return [
            {"indication_short": _short(part), "indication_full": part, "approval_country": "中国", "_from_pdf": True}
            for part in _split_numbered(m.group(1))
            if _short(part)
        ]
    except Exception:
        return []
