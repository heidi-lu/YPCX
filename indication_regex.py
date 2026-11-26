"""从医保局 PDF 的“说明书全部适应症”块按编号正则拆分；不调用大模型。"""
import re
import unicodedata
import pymupdf


_DECLARATION_COND_RE = re.compile(r"(?<!\d)\d{1,2}\.\s*20\d{2}年\d{1,2}月(?:\d{1,2}日)?[^。]*。")


def _strip_declaration_conditions(block: str) -> str:
    """删掉插在适应症中间的“申报条件”编号句，保留跨页续行。"""
    return _DECLARATION_COND_RE.sub("", block)


def _normalize_compat(text: str) -> str:
    """老 PDF 常把“用/入/门”写成康熙部首或兼容汉字，先归一化再匹配。"""
    out = []
    for ch in str(text or ""):
        code = ord(ch)
        if 0x2E80 <= code <= 0x2FDF or 0xF900 <= code <= 0xFAFF:
            out.append(unicodedata.normalize("NFKC", ch))
        else:
            out.append(ch)
    return "".join(out)


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_CIRCLED_RE = re.compile(rf"[{_CIRCLED}]")


def _split_circled(part: str) -> list[str]:
    """把【3】非小细胞肺癌：①非鳞状…②鳞状… 拆成两条，保留共同前缀。"""
    if len(_CIRCLED_RE.findall(part)) < 2:
        return [part]
    pieces = re.split(rf"(?=[{_CIRCLED}])", part)
    head = pieces[0].strip()
    subs = [p.strip() for p in pieces[1:] if p.strip()]
    return [(head + p) if head else p for p in subs]


_NUMBER_STYLES = (
    re.compile(r"【\s*(\d{1,2})\s*】"),
    re.compile(r"\[\s*(\d{1,2})\s*\]"),
    re.compile(r"(?<![\d.])(\d{1,2})\s*[.、]"),
    re.compile(r"(?<![\d.])(\d{1,2})\s*[)）]"),
    re.compile(r"[（(]\s*(\d{1,2})\s*[）)]"),
)


def _split_by_number_style(block: str, pattern) -> list[str] | None:
    matches = list(pattern.finditer(block))
    if len(matches) < 2:
        return None
    # “其中包括：1）…2）…”是同一适应症下的子条件，不能拆成独立适应症
    prefix = block[:matches[0].start()]
    if re.search(r"(?:包括|如下|以下)\s*[：:]?\s*$", prefix):
        return None
    selected, expected = [], 1
    for match in matches:
        try:
            num = int(match.group(1))
        except (IndexError, ValueError):
            continue
        if num == expected:
            selected.append(match)
            expected += 1
    if len(selected) < 2:
        return None
    parts = []
    for idx, match in enumerate(selected):
        end = selected[idx + 1].start() if idx + 1 < len(selected) else len(block)
        parts.append(block[match.start():end].strip())
    return parts


_HEADING_RE = re.compile(r"【[^】\d]{2,20}】|\[[^\]\d]{2,20}\]")
_HEADING_OK_RE = re.compile(r"(癌|瘤|病|综合征|肌无力|联合治疗|单药治疗|适应症|功能主治|治疗方案)")


def _heading_matches(block: str):
    out = []
    for m in _HEADING_RE.finditer(block):
        text = m.group(0)
        inner = text.strip("【】[]")
        # 【…】是强标题；[…]只认含中文的病名，避免把 [CPS]/[MSI-H] 当标题
        if text.startswith("【") or re.search(r"[\u4e00-\u9fa5]", inner):
            out.append(m)
    return out


def _split_by_headings(block: str) -> list[str] | None:
    matches = _heading_matches(block)
    if len(matches) < 2:
        return None
    starts = [m.start() for m in matches]
    parts = [block[a:b].strip() for a, b in zip(starts, starts[1:] + [len(block)])]
    # 去掉紧挨着下一个【标题】之前残留的序号，如“…。2.”
    parts = [re.sub(r"\d+\s*[.、]?\s*$", "", p).strip() for p in parts]
    prefix = re.sub(r"\d+[.、]?$", "", block[:starts[0]].strip()).strip()
    if not prefix.endswith("："):
        prefix = ""
    expanded = []
    for part in parts:
        heading_match = _HEADING_RE.match(part)
        heading = (heading_match.group(0).strip("【】[]") if heading_match else "")
        if heading in {"联合治疗", "单药治疗"}:
            # 疾病为主：把“非小细胞肺癌：”前缀补回去；单药治疗后面的其他病名再按标签拆开
            for sub in (_split_by_labels(part) or [part]):
                if _HEADING_RE.match(sub) and prefix:
                    sub = prefix + sub
                expanded.append(sub)
            continue
        for sub in _split_circled(part):
            labelled = _split_by_labels(sub)
            expanded.extend(labelled if labelled else [sub])
    return expanded


def _split_by_disease_before_product(block: str) -> list[str] | None:
    """病名直接接“本品”（没有冒号/编号）的说明书，如“宫颈癌本品适用于…”。"""
    parts = [p.strip() for p in re.split(
        r"(?<=[。；;])\s*(?=[\u4e00-\u9fa5A-Za-z0-9\-/（）()]{2,20}本品)", block) if p.strip()]
    return parts if len(parts) >= 2 else None


def _split_by_labels(block: str) -> list[str] | None:
    parts = [p.strip() for p in re.split(
        r"(?<=[。；;])\s*(?=[\u4e00-\u9fa5A-Za-z0-9\-/]{2,20}[：:])", block) if p.strip()]
    return parts if len(parts) >= 2 else None


def _split_numbered(block: str) -> list[str]:
    block = re.sub(r"\s+", "", block)
    block = _strip_declaration_conditions(block)
    # 【联合治疗】/【单药治疗】这类总标题优先整体成行，不按 ①② 拆
    generic_headings = [
        m.group(0) for m in _heading_matches(block) if m.group(0).strip("【】[]") in {"联合治疗", "单药治疗"}
    ]
    if len(generic_headings) >= 2:
        return _split_by_headings(block) or [block]
    # 编号样式按优先级依次尝试：有【1】就不用 [1]，有 [1] 就不用 1.，以此类推
    for pattern in _NUMBER_STYLES:
        parts = _split_by_number_style(block, pattern)
        if parts:
            return [sub for p in parts for sub in _split_circled(p)]
    # 没有序号、只有【病名】/[病名] 小标题的说明书
    parts = _split_by_headings(block)
    if parts:
        return parts
    if len(_CIRCLED_RE.findall(block)) >= 2:
        return _split_circled(block)
    parts = _split_by_disease_before_product(block)
    if parts:
        return parts
    parts = _split_by_labels(block)
    if parts:
        return parts
    # 没有编号：只在“本品适用于/用于/与常规…联合用于”开头处拆，避免把
    # “包括……”这类子人群说明拆成独立适应症
    parts = [p.strip() for p in re.split(
        r"(?<=[。；;])\s*(?=(?:本品)?(?:适用于|用于|与常规治疗药物联合用于))", block) if p.strip()]
    return [sub for p in (parts or [block]) for sub in _split_circled(p)]


_DISEASE_SUFFIX_RE = re.compile(r"(?:癌|瘤|病|疮|炎|综合征|肌无力|哮喘|息肉|感染|紫癜|硬化|纤维化)$")
_ABBR_BRACKET_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,30})[（(][A-Za-z0-9\-]{2,10}[）)]")
_ABBR_LEAD_RE = re.compile(r"^(?:适用于|用于|降低|存在|疾病|进展|风险|的|在|对|合并)+")


def _looks_like_disease_name(text: str) -> bool:
    name = str(text or "").strip("：:，,、；;。 ")
    if not name or name in {"疾病", "病情", "病变", "患病"} or name.endswith("高疾病"):
        return False
    return bool(_DISEASE_SUFFIX_RE.search(name))


def _short(part: str) -> str:
    m_prefix = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9\-/]{2,20})[：:]", part)
    if m_prefix and _looks_like_disease_name(m_prefix.group(1)):
        return m_prefix.group(1)

    m_heading = re.match(r"^(?:【([^】]+)】|\[([^\]]+)\])", part)
    if m_heading:
        heading = re.sub(r"\s+", "", m_heading.group(1) or m_heading.group(2) or "")
        if heading and not heading.isdigit():
            if heading in {"联合治疗", "单药治疗"}:
                return heading
            generic_headings = {"适应症", "功能主治", "用法用量", "用法", "治疗方案"}
            if heading not in generic_headings:
                return heading
            # “【联合治疗】/【单药治疗】”这类总标题，继续看后面的正文
            part = part[m_heading.end():].lstrip("：: ").strip()
    body = re.sub(r"^(?:【\d+】|\[\d+\]|[（(]?\d{1,2}\s*[)）.、:：]|[①②③④⑤⑥⑦⑧⑨⑩])", "", part).strip()
    short = re.split(r"本品|用于|适用|联合|（|\(", body, maxsplit=1)[0].strip()
    short = short.strip("：:，,、；;。 ")
    if not short:
        # “原发性免疫球蛋白A肾病（IgAN）”这类括号缩写：取括号前的病名
        m_abbr = _ABBR_BRACKET_RE.search(body)
        if m_abbr:
            candidate = _ABBR_LEAD_RE.sub("", m_abbr.group(1))
            if _looks_like_disease_name(candidate):
                short = candidate
    if short and not _looks_like_disease_name(short):
        cands = [
            c for c in re.findall(r"([\u4e00-\u9fa5]{2,12}(?:癌|瘤|病))", body)
            if not c.endswith(("疾病", "病情", "病变"))
        ]
        if "相关适应症" in body and len(set(cands)) >= 2:
            short = "其他癌症"
        elif cands:
            short = cands[0]
    if not short:
        candidates = re.findall(r"(?<=[，。；的性])([\u4e00-\u9fa5]{2,20}(?:癌|瘤|病))", body)
        if not candidates:
            candidates = re.findall(r"([\u4e00-\u9fa5]{2,12}(?:癌|瘤|病|疹|炎|哮喘))", body)
        if not candidates:
            candidates = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9]{2,30}(?:疾病|肌无力|综合征|谱系疾病))", body)
        if candidates:
            short = candidates[-1]
    if not short:
        tail = re.sub(r"^本品(?:适用于|用于)?(?:治疗)?", "", body).split("。")[0]
        tail = re.split(r"[，,；;：:]", tail)[0].strip()
        tail = re.sub(r"的?(?:成人|儿童)?患者.*$", "", tail).strip()
        if 1 < len(tail) <= 20:
            short = tail
    short = re.sub(r"^本品(?:适用于|用于)?", "", short)
    for prefix in ("的", "治疗", "抗体阳性的", "成人", "儿童", "患者"):
        while short.startswith(prefix):
            short = short[len(prefix):]
    short = re.sub(r"^的", "", short)
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
    cands = re.findall(r"([\u4e00-\u9fa5]{2,12}(?:癌|瘤|病))", body)
    if "相关适应症" in body and len(set(cands)) >= 2:
        short = "其他癌症"
    for prefix in ("复发或转移性", "复发或难治性", "不可切除或转移性", "局部晚期或转移性", "晚期", "经典型"):
        if short.startswith(prefix):
            short = short[len(prefix):]
    short = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩]\s*", "", short)
    return short.strip("：:，,、；;。 ")


# ===== 简称碎片修复：只靠后缀类和结构边界，不依赖病名词典 =====
DISEASE_TAIL = r"癌|瘤|病|炎|疹|哮喘|息肉|综合征|肌无力|感染|疮|紫癜|硬化|纤维化"
_DISEASE_TAIL_RE = re.compile(DISEASE_TAIL)
# 候选片段里出现这些词，说明抓到的是修饰语/治疗过程，不是病名
_BAD_IN_CANDIDATE = (
    "无法", "充分", "不能", "控制", "药物", "疾病", "该病", "此病", "本病",
    "既往", "失败", "进展", "化疗",
)
_CANDIDATE_STOP_CHARS = set("的和与及或，、；。：:（）()“”\"' 0123456789")
_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fa5·]")
_TREATMENT_ANCHOR_RE = re.compile(r"(?:联合)?(?:适用于|用于治疗|用于)")
_VERB_TAIL_RE = re.compile(r"(?:联合|治疗|用于|适用于|接受|患有|诊断为)$")
# 病名后缀后面允许跟的字/词；跟“激酶/受体/蛋白”说明只是词根
_RIGHT_OK_RE = re.compile(
    r"^(?:$|患者|成人|儿童|儿科|的|，|、|；|。|（|\(|及|和|或|等|相关|治疗|一线|二线|三线|围|术后|期|用|联|单|以|后|前|,)"
)
_LEFT_TRIM = ("成年", "成人", "儿童", "的", "期", "、")
_STAGE_PREFIXES = (
    "复发或转移性", "不可切除或转移性", "不可切除或晚期", "局部晚期或转移性",
    "复发性局部晚期或转移性", "原发性晚期或复发性", "转移性", "复发性", "局部晚期", "晚期", "经典型",
)
_PATIENT_TAIL_RE = re.compile(r"(?:儿科及成人|儿童及成人|成人及儿童|成人|儿童|儿科|老年)?患者$")
_DISEASE_TOKEN_RE = re.compile(rf"[\u4e00-\u9fa5]{{2,12}}(?:{DISEASE_TAIL})")
_ASCII_RE = re.compile(r"[A-Za-z]")


def _clean_for_short(full) -> str:
    text = re.sub(r"^[【\[]?\d{1,2}[】\])）.、:：]?", "", str(full or "").strip())
    return re.sub(r"[（(][^）)]*[）)]", "", text)


def strip_short_patient_tail(short: str) -> str:
    return _PATIENT_TAIL_RE.sub("", str(short or "").strip()).strip()


def _extend_left(text: str, end: int) -> int:
    i = end
    while i > 0:
        ch = text[i - 1]
        if not _CN_CHAR_RE.match(ch) or ch in _CANDIDATE_STOP_CHARS:
            break
        if _VERB_TAIL_RE.search(text[max(0, i - 2):i]):
            break
        i -= 1
    return i


def _first_disease_in(text: str) -> str:
    for m in _DISEASE_TAIL_RE.finditer(text):
        if not _RIGHT_OK_RE.match(text[m.end():]):
            continue
        start = _extend_left(text, m.start())
        candidate = text[start:m.end()]
        for prefix in _LEFT_TRIM:
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
        if len(candidate) < 2 or any(bad in candidate for bad in _BAD_IN_CANDIDATE):
            continue
        extra, end = "", m.end()
        while True:
            connector = re.match(
                rf"(伴|合并|及|和|或)([\u4e00-\u9fa5·]{{1,10}}?(?:{DISEASE_TAIL}))", text[end:]
            )
            if not connector:
                break
            extra += connector.group(1) + connector.group(2)
            end += connector.end()
        return candidate + extra
    return ""


def _normalize_short_candidate(candidate: str, full) -> str:
    if "相关适应症" in str(full) and len(set(_DISEASE_TOKEN_RE.findall(str(full)))) >= 2:
        return "其他癌症"
    for prefix in _STAGE_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    if "非小细胞肺癌" in candidate:
        return "非小细胞肺癌"
    if "小细胞肺癌" in candidate:
        return "小细胞肺癌"
    return candidate


def disease_short_from_full(full) -> str:
    """从一个分段后的适应症里取病名：括号同位语 → 治疗动词后 → 后缀+边界。"""
    text = _clean_for_short(full)
    anchor = _TREATMENT_ANCHOR_RE.search(text)
    tails = []
    if anchor:
        tails.append(text[anchor.end():])
    tails.append(text)
    for tail in tails:
        got = _first_disease_in(tail)
        if got:
            return _normalize_short_candidate(got, full)
    return ""


_FRAGMENT_START_RE = re.compile(r"^(?:或|与|作为|当|对于|既往|在接受|经过|经充分|经铂|经含铂)")
_FRAGMENT_MID_RE = re.compile(r"无法|未充分|不能控制|药物")


def looks_like_short_fragment(short: str, full) -> bool:
    """简称是否为碎片；纯英文缩写（如 III期不可切NSCLC）不判碎片。"""
    s = strip_short_patient_tail(short)
    f = str(full or "").strip()
    if not s:
        return True
    if _FRAGMENT_START_RE.match(s) or _FRAGMENT_MID_RE.search(s):
        return True
    if re.search(r"(?:治疗|患者|联合|含|或|与|和|及)$", s):
        return True
    if not _ASCII_RE.search(s):
        if s == f and len(s) > 16:
            return True
        if len(s) > 20:
            return True
        if not _DISEASE_TAIL_RE.search(s) and _DISEASE_TAIL_RE.search(f):
            return True
    return False


def extract_indications_from_pdf(pdf_bytes: bytes) -> list[dict]:
    try:
        text = "\n".join(p.get_text() for p in pymupdf.open(stream=pdf_bytes, filetype="pdf"))
        text = _normalize_compat(text)
        m = re.search(
            r"说明书全部适应症(?:/功能主治)?\s*(.*?)(?:现行医保目录|说明书用法用量|所治疗疾病|药品申报条件|申报条件)",
            text,
            re.S,
        )
        if not m:
            return []
        block = m.group(1)
        # 申报条件常被 PDF 排版插在适应症中间（甚至跨页），只删这几句编号条件，
        # 不能用 (?=【|$) 一路吞到结尾，否则会把下一页的适应症续行一起删掉
        block = _strip_declaration_conditions(block)
        block = re.sub(r"\s+", "", block)
        return [
            {"indication_short": _short(part), "indication_full": part, "approval_country": "中国", "_from_pdf": True}
            for part in _split_numbered(block)
            if _short(part)
        ]
    except Exception:
        return []
