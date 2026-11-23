import re
import requests
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

NHSA_PUBLIC_PAGES = {
    2026: "https://www.nhsa.gov.cn/art/2026/6/29/art_152_21132.html",
    2025: "https://www.nhsa.gov.cn/art/2025/8/12/art_152_17558.html",
    2024: "https://www.nhsa.gov.cn/art/2024/8/7/art_152_13497.html",
    2023: "https://www.nhsa.gov.cn/art/2023/8/18/art_152_11182.html",
    2022: "https://www.nhsa.gov.cn/art/2022/9/6/art_152_8853.html",
}

def fetch_public_page(year: int) -> str:
    url = NHSA_PUBLIC_PAGES.get(year)
    if not url: return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if not resp.encoding or resp.encoding.upper() in ("ISO-8859-1", "LATIN-1"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception:
        return ""

def _make_absolute(href: str) -> str:
    if href.startswith("http"): return href
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"): return "https://www.nhsa.gov.cn" + href
    return "https://www.nhsa.gov.cn/" + href

def extract_file_links(html: str, drug_name: str, year: int) -> list:
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(strip=True)
        parent_text = a.parent.get_text(separator=" ", strip=True) if a.parent else link_text
        
        if drug_name not in parent_text and drug_name not in link_text: continue
        full_url = _make_absolute(href)

        lower = href.lower()
        is_ppt = ("ppt" in lower) or ("摘要" in link_text)
        is_pdf = lower.endswith(".pdf") or ("pdf" in lower and "说明书" in link_text)
        if not (is_ppt or is_pdf): continue

        m_clean = re.search(r'[A-Z0-9]+-([^：:]+)[：:]', parent_text)
        clean_name = m_clean.group(1).strip() if m_clean else parent_text.split("：")[0].replace("YPSW", "").strip()

        if not any(r["url"] == full_url for r in results):
            results.append({
                "url": full_url, "type": "ppt" if is_ppt else "pdf", 
                "text": link_text, "context": parent_text,
                "clean_name": clean_name, "year": year
            })
    return results

def _name_variants(drug_name: str) -> list:
    """药名容错：组合名多余的“单抗”、剂型后缀，都可能让官方检索落空。"""
    base = str(drug_name or "").strip()
    variants = [base]
    # 艾帕洛利单抗托沃瑞利单抗 → 艾帕洛利托沃瑞利单抗
    if base.count("单抗") >= 2:
        i = base.find("单抗")
        variants.append(base[:i] + base[i + 2:])
    # 有/无剂型后缀各试一次
    for suffix in ("注射液", "注射剂", "片", "胶囊"):
        if base.endswith(suffix):
            variants.append(base[: -len(suffix)])
    if not base.endswith(("注射液", "注射剂", "片", "胶囊")):
        variants.append(base + "注射液")
    return [v for v in dict.fromkeys(variants) if len(v) >= 2]


def _find_exact(drug_name: str, years: list) -> list:
    final_files, seen_urls = [], set()
    for year in years:
        html = fetch_public_page(year)
        if not html: continue
        for item in extract_file_links(html, drug_name, year):
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                final_files.append(item)
    return final_files


def find_drug_files(drug_name: str, years: list = None) -> list:
    if years is None: years = [2026, 2025, 2024, 2023, 2022]
    for variant in _name_variants(drug_name):
        files = _find_exact(variant, years)
        if files:
            return files
    return []