#!/usr/bin/env python3
"""Parse the 2026 commercial-health innovative-drug PDF into the NHI CSV schema."""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import pdfplumber

from parse_nhsa_2026_pdf import clean, clean_name, code_level, infer_form


def parse_pdf(pdf_path: Path) -> list[dict]:
    rows: list[dict] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        header = None
        path: dict[int, str] = {}
        for page_index, page in enumerate(pdf.pages):
            if page_index == 0:
                continue
            tables = page.extract_tables(
                table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}
            )
            if not tables:
                continue
            for table in tables:
                local_header = None
                for raw_row in table[:3]:
                    cells = [clean(cell) for cell in raw_row]
                    if "药品名称" in cells and "编号" in cells:
                        header = cells
                        local_header = cells
                        break
                if local_header is None:
                    local_header = header
                if not local_header:
                    continue
                try:
                    number_index = local_header.index("编号")
                    name_index = local_header.index("药品名称")
                except ValueError:
                    continue
                indication_index = (
                    local_header.index("适应症") if "适应症" in local_header else None
                )
                for raw_row in table:
                    cells = [clean(cell) for cell in raw_row]
                    cells += [""] * (len(local_header) - len(cells))
                    if "药品名称" in cells or "药品分类代码" in cells:
                        continue
                    code = ""
                    level = 0
                    for column in range(number_index):
                        candidate = cells[column]
                        candidate_level, ok = code_level(candidate)
                        if ok:
                            code = candidate
                            level = candidate_level
                            break
                    if code:
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
                    if not re.search(r"\d", number or ""):
                        continue
                    name = clean_name(cells[name_index]) if name_index < len(cells) else ""
                    if not name:
                        continue
                    note = (
                        cells[indication_index]
                        if indication_index is not None and indication_index < len(cells)
                        else ""
                    )
                    rows.append(
                        {
                            "page": page_index + 1,
                            "number": number,
                            "name": name,
                            "form": infer_form(name),
                            "category": path[max(path)] if path else "商业健康保险创新药",
                            "note": note,
                        }
                    )
    unique: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        key = (row["number"], row["name"], row["note"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


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
                    "目录类型": "商业健康保险创新药",
                    "年份": "2026",
                    "执行时间": "2027年1月1日",
                    "药品名称（完整通用名）": row["name"],
                    "剂型": row["form"],
                    "药品分类": row["category"],
                    "医保类别": "",
                    "医保支付标准": "",
                    "医保限制（备注）": row["note"],
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf",
        default="data/2026_import/nhsa_2026_commercial.pdf",
    )
    parser.add_argument(
        "--csv",
        default="data/2026_import/2026年商业健康保险创新药品目录.csv",
    )
    args = parser.parse_args()
    rows = parse_pdf(Path(args.pdf))
    write_csv(rows, Path(args.csv))
    print(f"rows: {len(rows)}")
    for row in rows[:5]:
        print(row)
    for row in rows[-5:]:
        print(row)


if __name__ == "__main__":
    main()
