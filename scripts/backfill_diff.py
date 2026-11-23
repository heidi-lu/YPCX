"""存量回填预演：只读数据库，输出“新规范化 vs 当前值”的 diff。

用法：
    ./venv/bin/python scripts/backfill_diff.py                # 打印摘要 + 写 reports/
    ./venv/bin/python scripts/backfill_diff.py --json out.json
    ./venv/bin/python scripts/backfill_diff.py --limit 20     # 只看前 20 条样例
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

import main  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import Drug  # noqa: E402

JUNK_PATTERNS = ("患者", "联用", "Keytruda", "治疗")


def _raw_short(value: str) -> str:
    text = re.sub(r"[（(][^）)]*[）)]", "", str(value or "")).strip()
    return re.sub(r"^(降低|存在|疾病进展|风险的)+", "", text).strip()


def _is_junk(short: str, full: str) -> bool:
    short = str(short or "")
    if not short:
        return False
    if short == str(full or ""):
        return True
    if len(short) > 20:
        return True
    return any(word in short for word in JUNK_PATTERNS)


def build_rows(limit: int = 0) -> list[dict]:
    with SessionLocal() as session:
        rows = session.query(Drug).order_by(Drug.id).all()
        data = []
        for row in rows:
            old_short = str(row.indication_short or "")
            old_subtype = str(row.indication_subtype or "")
            full = str(row.indication_full or "")
            raw = _raw_short(old_short)
            new_short, new_subtype = main._split_short_subtype(raw, full)
            data.append({
                "id": row.id,
                "generic_name": row.generic_name,
                "brand_name": row.brand_name,
                "country": row.approval_country,
                "old_short": old_short,
                "new_short": new_short,
                "old_subtype": old_subtype,
                "new_subtype": new_subtype,
                "full": full,
                "junk": _is_junk(old_short, full),
                "changed": (old_short != new_short) or (old_subtype != new_subtype),
            })
    return data[:limit] if limit > 0 else data


def summarize(rows: list[dict]) -> dict:
    changed = [row for row in rows if row["changed"]]
    junk = [row for row in rows if row["junk"]]
    dup_counter = Counter((row["generic_name"], row["old_short"]) for row in rows)
    dup_groups = {key: count for key, count in dup_counter.items() if count > 1}
    per_drug = defaultdict(lambda: {"rows": 0, "changed": 0, "junk": 0})
    for row in rows:
        bucket = per_drug[row["generic_name"]]
        bucket["rows"] += 1
        bucket["changed"] += int(row["changed"])
        bucket["junk"] += int(row["junk"])
    return {
        "total_rows": len(rows),
        "changed_rows": len(changed),
        "junk_rows": len(junk),
        "dup_groups": len(dup_groups),
        "rows_in_dup_groups": sum(dup_groups.values()),
        "per_drug": dict(sorted(per_drug.items(), key=lambda kv: -kv[1]["changed"])),
        "changed": changed,
        "junk": junk,
    }


def render_markdown(summary: dict, samples: int = 40) -> str:
    lines = [
        f"# 存量回填 diff（{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
        "> 只读预演，未写库。新值来自当前代码的 `_split_short_subtype`。",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 总行数 | {summary['total_rows']} |",
        f"| 简称/分型会变 | {summary['changed_rows']} |",
        f"| 碎片行 | {summary['junk_rows']} |",
        f"| 重复组 | {summary['dup_groups']}（{summary['rows_in_dup_groups']} 行） |",
        "",
        "## 变更样例",
        "",
        "| 药品 | 旧简称 | 新简称 | 旧分型 | 新分型 |",
        "|---|---|---|---|---|",
    ]
    for row in summary["changed"][:samples]:
        lines.append(
            f"| {row['generic_name']} | {row['old_short']} | {row['new_short']} "
            f"| {row['old_subtype'] or '-'} | {row['new_subtype'] or '-'} |"
        )
    if not summary["changed"]:
        lines.append("| - | - | - | - | - |")
    lines += ["", "## 碎片行", "", "| 药品 | 简称 | 全文 |", "|---|---|---|"]
    for row in summary["junk"][:samples]:
        lines.append(f"| {row['generic_name']} | {row['old_short']} | {row['full'][:80]} |")
    if not summary["junk"]:
        lines.append("| - | - | - |")
    lines.append("")
    return "\n".join(lines)


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="", help="把完整 diff 写到这个 JSON 文件")
    parser.add_argument("--md", default="", help="Markdown 报告路径；默认写 reports/")
    parser.add_argument("--limit", type=int, default=0, help="只看前 N 行（调试用）")
    args = parser.parse_args()

    with SessionLocal() as session:
        before = session.query(func.count(Drug.id)).scalar()

    rows = build_rows(limit=args.limit)
    summary = summarize(rows)

    md_path = args.md or os.path.join(
        "reports", f"backfill_diff_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    )
    os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(summary))

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)

    with SessionLocal() as session:
        after = session.query(func.count(Drug.id)).scalar()

    print(f"总行数 {summary['total_rows']}，会变 {summary['changed_rows']}，"
          f"碎片 {summary['junk_rows']}，重复组 {summary['dup_groups']}（{summary['rows_in_dup_groups']} 行）")
    print(f"报告: {md_path}")
    print(f"行数校验: before={before} after={after}（必须相等）")
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
