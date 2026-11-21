#!/usr/bin/env python3
"""Parse the official 2026 NRDL PDF into the existing NHI CSV schema.

This is a staging tool: it writes CSV + reconciliation report and does NOT
touch nhi_dictionary.json or build_dict.py.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
from pathlib import Path

import pdfplumber

# PDF page indexes (0-based) from the official 186-page PDF's table of contents.
# 西药 p8, 中成药 p81, 谈判 p116, 竞价 p171, 饮片 p175 (printed page numbers).
SECTIONS = [
    (8, 80, "西药"),
    (81, 115, "中成药"),
    (116, 170, "协议期内谈判药品"),
    (171, 174, "竞价药品"),
    (175, 185, "中药饮片"),
]

# Four duplicate numbers in the PDF. The four rows are present and their names
# fit the missing sequence; correct only for the reconciliation count.
NUMBER_FIXES = {
    (82, "小儿感冒舒颗粒"): "28",
    (82, "小儿感冒颗粒"): "29",
    (82, "小儿感冒退热糖浆"): "30",
    (88, "强力枇杷露(胶囊、颗粒)"): "231",
    (89, "蛇胆川贝枇杷膏"): "258",
}

FORM_PATTERNS = [
    (r"注射用|注射液$|注射剂$|注射溶液$|眼内注射溶液$", "注射剂"),
    (r"干混悬剂|口服混悬剂", "口服混悬剂"),
    (r"粉雾剂$", "粉雾剂"),
    (r"吸入用|吸入溶液$|雾化吸入溶液$", "吸入剂"),
    (r"植入剂$", "植入剂"),
    (r"乳剂$", "乳剂"),
    (r"透析液$", "透析液"),
    (r"置换液$", "置换液"),
    (r"冲洗液$|冲洗剂$", "冲洗剂"),
    (r"涂剂$|涂膜剂$", "涂剂"),
    (r"搽剂$", "搽剂"),
    (r"洗剂$|洗液$|药水$", "洗剂"),
    (r"滴耳剂$", "滴耳剂"),
    (r"眼膏$", "眼膏剂"),
    (r"口溶膜$", "口溶膜"),
    (r"灌肠液$", "灌肠剂"),
    (r"妇洁液$|固龈液$", "洗剂"),
    (r"液$|汤$|浆$|饮$", "口服液体剂"),
    (r"贴$", "贴剂"),
    (r"膜$|贴膜$|巴布膏$", "贴剂"),
    (r"灌肠剂$", "灌肠剂"),
    (r"凝胶$", "凝胶剂"),
    (r"乳胶$", "乳胶剂"),
    (r"喷雾剂$", "喷雾剂"),
    (r"气雾剂$", "气雾剂"),
    (r"滴剂$", "滴剂"),
    (r"含漱液$", "含漱液"),
    (r"缓释片|控释片|肠溶片|咀嚼片|分散片|口崩片|口腔崩解片|泡腾片|含片|片$", "片剂"),
    (r"软胶囊|肠溶胶囊|缓释胶囊|胶囊$", "胶囊"),
    (r"颗粒|颗粒剂$", "颗粒剂"),
    (r"口服溶液|口服液|混悬液|糖浆|溶液$", "口服液体剂"),
    (r"散$|散剂$", "散剂"),
    (r"滴丸|丸$|丹$", "丸剂"),
    (r"锭$", "锭剂"),
    (r"曲$", "曲剂"),
    (r"胶$", "胶剂"),
    (r"茶$", "茶剂"),
    (r"霜$", "霜剂"),
    (r"膏药$|膏剂$|膏$", "膏剂"),
    (r"酊$", "酊剂"),
    (r"油$", "油剂"),
    (r"水$|露$|合剂$", "口服液体剂"),
    (r"软膏|乳膏", "软膏剂"),
    (r"滴眼液|眼用凝胶", "滴眼剂"),
    (r"贴剂|贴膏", "贴剂"),
    (r"栓$|栓剂$", "栓剂"),
    (r"组合包装$", "组合包装"),
]


def clean(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\u3000", " ")).strip()


def clean_name(value) -> str:
    """PDF line wraps insert spaces inside Chinese drug names."""
    return re.sub(r"\s+", "", clean(value))


def infer_form(name: str) -> str:
    # Dosage forms often follow a strength/roman-numeral suffix: 片(Ⅱ), 注射液(18AA-SF)
    base = re.sub(r"[（(][^（）()]*[）)]\s*$", "", name)
    for pattern, form in FORM_PATTERNS:
        if re.search(pattern, base):
            return form
    return "未分类"


def code_level(code: str) -> tuple[int, bool]:
    match = re.fullmatch(r"([A-Z]+)(\d*)([A-Z]*)", code or "")
    if not match:
        return 0, False
    return 1 + (1 if match.group(2) else 0) + len(match.group(3)), True


def section_for(page_index: int) -> str:
    for low, high, name in SECTIONS:
        if low <= page_index <= high:
            return name
    return ""


def parse_pdf(pdf_path: Path) -> list[dict]:
    rows: list[dict] = []
    errors: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        section = ""
        path: dict[int, str] = {}
        last_header: list[str] | None = None
        last_kind: str | None = None
        prefix = ""
        for page_index, page in enumerate(pdf.pages):
            new_section = section_for(page_index)
            if new_section and new_section != section:
                section = new_section
                path = {}
                last_header = None
                last_kind = None
                prefix = ""
            if page_index < 8:
                continue

            tables = []
            for strategy in ("lines", "text"):
                try:
                    extracted = page.extract_tables(
                        table_settings={
                            "vertical_strategy": strategy,
                            "horizontal_strategy": "lines",
                        }
                    )
                except Exception as exc:  # pragma: no cover - PDF-specific fallback
                    errors.append(f"page {page_index + 1}: {exc}")
                    extracted = []
                tables = [table for table in (extracted or []) if table]
                if tables:
                    break
            if not tables:
                continue

            for table in tables:
                header = None
                kind = None
                for raw_row in table[:4]:
                    cells = [clean(cell) for cell in raw_row]
                    if "药品名称" in cells or "饮片名称" in cells:
                        header = cells
                        last_header = cells
                        last_kind = "饮片" if "饮片名称" in cells else "药品"
                        kind = last_kind
                        break
                if header is None:
                    header = last_header
                    kind = last_kind
                if not header:
                    continue

                if kind == "饮片":
                    for raw_row in table:
                        cells = [clean(cell) for cell in raw_row]
                        if cells and cells[0] == "序号":
                            continue
                        for offset in (0, 3):
                            if len(cells) <= offset + 1:
                                continue
                            number = cells[offset]
                            name = clean_name(cells[offset + 1])
                            note = cells[offset + 2] if len(cells) > offset + 2 else ""
                            if not re.fullmatch(r"\d+", number or "") or not name:
                                continue
                            rows.append(
                                {
                                    "section": "中药饮片",
                                    "page": page_index + 1,
                                    "number": number,
                                    "name": name,
                                    "form": "饮片",
                                    "category": "中药饮片",
                                    "insurance_category": "",
                                    "price": "",
                                    "note": note,
                                }
                            )
                    continue

                try:
                    number_index = header.index("编号")
                except ValueError:
                    continue
                insurance_index = number_index - 1
                name_index = (
                    header.index("药品名称") if "药品名称" in header else number_index + 1
                )
                form_index = header.index("剂型") if "剂型" in header else None
                note_index = header.index("备注") if "备注" in header else None
                price_index = (
                    header.index("医保支付标准") if "医保支付标准" in header else None
                )

                for raw_row in table:
                    cells = [clean(cell) for cell in raw_row]
                    cells += [""] * (len(header) - len(cells))
                    if "药品名称" in cells or "药品分类代码" in cells:
                        continue

                    code = ""
                    level = 0
                    for column in range(max(0, number_index)):
                        candidate = cells[column]
                        candidate_level, ok = code_level(candidate)
                        if ok:
                            code = candidate
                            level = candidate_level
                            break
                    if code:
                        prefix = code[:1]
                        category_name = ""
                        for column in range(1, number_index):
                            value = cells[column]
                            if value and not code_level(value)[1]:
                                category_name = value
                                break
                        if category_name:
                            path[level] = category_name
                            for key in list(path):
                                if key > level:
                                    path.pop(key, None)
                        continue

                    number = cells[number_index] if number_index < len(cells) else ""
                    if not number or not re.search(r"\d", number):
                        continue
                    name = clean_name(cells[name_index]) if name_index < len(cells) else ""
                    if not name:
                        for column in range(number_index + 1, len(cells)):
                            if cells[column]:
                                name = clean_name(cells[column])
                                break
                    if not name:
                        continue

                    insurance_category = (
                        cells[insurance_index]
                        if 0 <= insurance_index < len(cells)
                        else ""
                    )
                    if insurance_category not in ("甲", "乙"):
                        insurance_category = ""
                    if form_index is not None and form_index < len(cells) and cells[form_index]:
                        form = cells[form_index]
                    else:
                        form = infer_form(name)
                    note = (
                        cells[note_index]
                        if note_index is not None and note_index < len(cells)
                        else ""
                    )
                    price = (
                        cells[price_index]
                        if price_index is not None and price_index < len(cells)
                        else ""
                    )
                    rows.append(
                        {
                            "section": section,
                            "page": page_index + 1,
                            "number": number,
                            "name": name,
                            "form": form,
                            "category": path[max(path)] if path else section,
                            "insurance_category": insurance_category,
                            "price": price,
                            "note": note,
                            "prefix": prefix,
                        }
                    )

    # Multiple tables on one page can overlap; keep first exact row.
    unique: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        key = (
            row["section"],
            row["page"],
            row["number"],
            row["name"],
            row["form"],
            row["note"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    if errors:
        print(f"parse warnings: {len(errors)}")
    return unique


def normalized_number(value: str) -> str:
    match = re.search(r"\d+", value or "")
    return match.group(0) if match else ""


def build_stats(rows: list[dict]) -> dict:
    official = {
        "西药": 1472,
        "中成药": 1328,
        "协议期内谈判药品": 515,
        "竞价药品": 16,
        "中药饮片": 885,
    }
    stats = {}
    for section, expected in official.items():
        section_rows = [row for row in rows if row["section"] == section]
        if section in ("协议期内谈判药品", "竞价药品"):
            numbers = {
                (row.get("prefix", ""), normalized_number(row["number"]))
                for row in section_rows
            }
        else:
            numbers = {normalized_number(row["number"]) for row in section_rows}
        stats[section] = {
            "rows": len(section_rows),
            "unique_names": len({row["name"] for row in section_rows}),
            "unique_numbers": len(numbers),
            "official": expected,
        }
        if section == "中药饮片":
            continue
        fixed_numbers = set(numbers)
        fixes = []
        for row in section_rows:
            corrected = NUMBER_FIXES.get((row["page"], row["name"]))
            if corrected:
                fixed_numbers.add(corrected)
                if corrected != normalized_number(row["number"]):
                    fixes.append(
                        {
                            "page": row["page"],
                            "name": row["name"],
                            "raw": row["number"],
                            "corrected": corrected,
                        }
                    )
        stats[section]["fixed_unique_numbers"] = len(fixed_numbers)
        stats[section]["number_fixes"] = fixes

    basic = [row for row in rows if row["section"] != "中药饮片"]
    stats["_basic"] = {
        "rows": len(basic),
        "unique_names": len({row["name"] for row in basic}),
        "unique_numbers": len(
            {(row["section"], row["prefix"], normalized_number(row["number"])) for row in basic}
        ),
    }
    stats["_all"] = {"rows": len(rows), "unique_names": len({row["name"] for row in rows})}
    stats["_unclassified_forms"] = [
        {"section": row["section"], "name": row["name"], "form": row["form"]}
        for row in rows
        if row.get("form") == "未分类"
    ]
    return stats


def write_csv(rows: list[dict], path: Path) -> None:
    headers = [
        "目录类型",
        "年份",
        "执行时间",
        "药品名称（完整通用名）",
        "剂型",
        "药品分类",
        "医保类别",
        "医保支付标准",
        "医保限制（备注）",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "目录类型": "基本医保",
                    "年份": "2026",
                    "执行时间": "2027年1月1日",
                    "药品名称（完整通用名）": row["name"],
                    "剂型": row["form"],
                    "药品分类": row["category"],
                    "医保类别": row["insurance_category"],
                    "医保支付标准": row["price"],
                    "医保限制（备注）": row["note"],
                }
            )


def write_report(rows: list[dict], stats: dict, path: Path) -> None:
    lines = [
        "# 2026 年国家医保药品目录 PDF 解析对账报告",
        "",
        "## 来源",
        "",
        "- 官方通知：https://www.nhsa.gov.cn/art/2026/11/14/art_53_23387.html",
        "- 官方 PDF：https://www.nhsa.gov.cn/module/download/downfile.jsp?classid=0&filename=95cee7b0c6544c0390fd4263456b5afd.pdf",
        "- 发布：2026-11-14；执行：**2027-01-01**",
        "- 本地解析文件：`data/2026_import/nhsa_2026_catalog.pdf`",
        "- 本报告只做 CSV 对账，未修改 `nhi_dictionary.json`。",
        "",
        "## 总量",
        "",
        f"- CSV 总行数：**{stats['_all']['rows']}**",
        f"- 唯一药品名称：**{stats['_all']['unique_names']}**",
        f"- 基本医保行数（不含饮片）：**{stats['_basic']['rows']}**",
        "",
        "| 部分 | 官方品种数 | CSV 行数 | 唯一名称 | PDF 唯一编号 | 修正后唯一编号 | 状态 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for section in ["西药", "中成药", "协议期内谈判药品", "竞价药品", "中药饮片"]:
        item = stats[section]
        fixed = item.get("fixed_unique_numbers", item["unique_numbers"])
        if section == "中药饮片":
            status = "✅" if item["rows"] == item["official"] else "⚠️"
        else:
            status = "✅" if fixed == item["official"] else "⚠️"
        lines.append(
            f"| {section} | {item['official']} | {item['rows']} | "
            f"{item['unique_names']} | {item['unique_numbers']} | {fixed} | {status} |"
        )
    lines += [
        "",
        "> 官方数字是“品种数”，CSV 行数包含同一编号下的不同剂型/★ 行，"
        "所以 CSV 行数大于官方品种数是正常的。",
        "",
        "## 编号异常",
        "",
        "PDF 中有 4 处编号重复/跳号；对应药品名称都在，CSV 不输出编号，"
        "这里按上下文顺序修正后用于对账：",
        "",
    ]
    for section in ["中成药"]:
        for fix in stats[section].get("number_fixes", []):
            lines.append(
                f"- 第 {fix['page']} 页：`{fix['name']}`，PDF 写作 `{fix['raw']}`，"
                f"按序列修正为 `{fix['corrected']}`"
            )
    lines += [
        "",
        "## 字段说明",
        "",
        "- `目录类型`：全部为 `基本医保`；2026 年商保创新药目录是另一份 PDF，未混入。",
        "- `年份`：`2026`，表示目录版本。",
        "- `执行时间`：`2027年1月1日`，不是 2026-01-01。",
        "- `剂型`：西药取 PDF 的剂型列；中成药 PDF 没有剂型列，按药名后缀推断；"
        "谈判/竞价同样按药名推断；饮片统一为 `饮片`。",
        "- `医保支付标准`：只有谈判/竞价部分有，其他为空。",
        "- `医保限制（备注）`：保留 PDF 原文，已清理换行。",
        "",
        "## 需要人工复核",
        "",
        f"- `未分类` 剂型：**{len(stats['_unclassified_forms'])}** 条；主要是中成药/谈判/竞价中无法从药名推断剂型的复方或特殊剂型。",
        "- 中药饮片 `医保类别` 为空，因为 PDF 饮片表没有甲乙类列。",
        "- 谈判/竞价剂型为推断值，后续进入字典前建议抽样核对。",
        "- 商保创新药目录未纳入本 CSV。",
    ]
    if stats["_unclassified_forms"]:
        lines += ["", "`未分类` 样例：", ""]
        for item in stats["_unclassified_forms"][:20]:
            lines.append(f"- {item['section']}：`{item['name']}`")
    lines += [
        "",
        "## 抽样",
        "",
    ]
    for keyword in ["贝利尤", "美泊利珠", "度普利尤", "司美格鲁肽"]:
        samples = [row for row in rows if keyword in row["name"]]
        if not samples:
            lines.append(f"- `{keyword}`：未找到")
            continue
        for row in samples[:3]:
            lines.append(
                f"- `{keyword}`：{row['name']} / {row['form']} / "
                f"{row['insurance_category']} / {row['note'][:80]}"
            )
    lines += [
        "",
        "## 下一步（未执行）",
        "",
        "1. 你确认 CSV 后，再把它放入 ` nhi_excels/`。",
        "2. 重建 `nhi_dictionary.json`，新增执行年 2027。",
        "3. 对账 2025 版目录（执行年 2026）与 2026 版目录（执行年 2027）的差异。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf",
        default="data/2026_import/nhsa_2026_catalog.pdf",
        help="official 2026 NRDL PDF",
    )
    parser.add_argument(
        "--csv",
        default="data/2026_import/2026年国家医保药品目录.csv",
    )
    parser.add_argument(
        "--report",
        default="data/2026_import/对账报告.md",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    rows = parse_pdf(pdf_path)
    stats = build_stats(rows)
    csv_path = Path(args.csv)
    report_path = Path(args.report)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, csv_path)
    write_report(rows, stats, report_path)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"CSV: {csv_path}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
