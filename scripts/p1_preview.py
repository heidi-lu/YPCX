"""P1 只读预演：按“能否自动写库”给 103 行分类，重点看 18 条 DRAFT。

用法：
    ./venv/bin/python scripts/p1_preview.py
输出：
    reports/p1_backfill_preview.md
    reports/p1_backfill_preview.json
不写 drugs 表。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backfill_diff  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import Drug  # noqa: E402

JUNK_WORDS = ("患者", "联用", "Keytruda")


def is_real_junk(short: str, full: str) -> bool:
    text = str(short or "")
    if len(text) > 20:
        return True
    if any(word in text for word in JUNK_WORDS):
        return True
    if text.endswith(("或含", "联合", "用于", "治疗")):
        return True
    return text == str(full or "") and len(text) > 12


def classify(row: dict) -> dict:
    old, new = row["old_short"], row["new_short"]
    old_sub, new_sub = row["old_subtype"], row["new_subtype"]
    row["real_junk"] = is_real_junk(new, row["full"])
    row["short_changed"] = old != new
    row["subtype_added"] = not old_sub and bool(new_sub)
    row["big_shorten"] = row["short_changed"] and len(old) - len(new) >= 4 and not new_sub
    row["sclc_collapse"] = "小细胞" in old and "小细胞" not in new
    row["safe_auto"] = (
        not row["real_junk"]
        and (bool(new_sub) or not row["short_changed"] or len(old) - len(new) < 4)
    )
    return row


def build() -> list[dict]:
    with SessionLocal() as session:
        records = {row.id: row for row in session.query(Drug).all()}
    rows = []
    for row in backfill_diff.build_rows():
        record = records.get(row["id"])
        row["status"] = getattr(record, "status", "")
        row["approval_country"] = getattr(record, "approval_country", "中国")
        rows.append(classify(row))
    return rows


def render(rows: list[dict]) -> str:
    total = len(rows)
    draft = [row for row in rows if row["status"] == "DRAFT"]
    changed = [row for row in rows if row["changed"]]
    draft_safe = [row for row in draft if row["safe_auto"] and row["changed"]]
    draft_noop = [row for row in draft if not row["changed"] and row["safe_auto"]]
    draft_hold = [row for row in draft if not row["safe_auto"]]
    hold = [row for row in rows if not row["safe_auto"]]
    sclc = [row for row in rows if row["sclc_collapse"]]
    kinds = Counter()
    for row in changed:
        if row["short_changed"]:
            kinds["简称变化"] += 1
        if row["subtype_added"]:
            kinds["新增分型"] += 1
        if row["real_junk"]:
            kinds["仍是碎片/长名"] += 1

    lines = [
        f"# P1 回填预演（{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
        "> 只读，不写库。判断基于当前 `_split_short_subtype` 与 `full` 文本。",
        "",
        "## 一、总体",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 总行数 | {total} |",
        f"| 有变化 | {len(changed)} |",
        f"| DRAFT | {len(draft)} |",
        f"| DRAFT 有变化且可自动写 | {len(draft_safe)} |",
        f"| DRAFT 无变化 | {len(draft_noop)} |",
        f"| DRAFT 需暂缓 | {len(draft_hold)} |",
        f"| 全库需暂缓 | {len(hold)} |",
        "",
        f"变化构成：简称变化 {kinds['简称变化']}，新增分型 {kinds['新增分型']}，变化后仍是碎片 {kinds['仍是碎片/长名']}。",
        f"全库仍有 {sum(1 for row in rows if row['real_junk'])} 行碎片/超长简称（含未变化的行）。",
        "",
        "## 二、DRAFT 逐行（18 条）",
        "",
        "| 药品 | 旧简称 | 新简称 | 新分型 | 判定 |",
        "|---|---|---|---|---|",
    ]
    for row in draft:
        verdict = "可自动写" if row["safe_auto"] else ("碎片/长名，暂缓" if row["real_junk"] else "丢限定词，暂缓")
        lines.append(
            f"| {row['generic_name']} | {row['old_short']} | {row['new_short']} "
            f"| {row['new_subtype'] or '-'} | {verdict} |"
        )

    lines += [
        "",
        "## 三、DRAFT 里建议自动写的",
        "",
    ]
    if draft_safe:
        for row in draft_safe:
            lines.append(f"- {row['generic_name']}：`{row['old_short']}` → `{row['new_short']}` / 分型 `{row['new_subtype'] or '-'}`")
    else:
        lines.append("- 无")

    lines += [
        "",
        "## 四、暂缓的行（全库）",
        "",
        "| 状态 | 药品 | 简称 | 原因 |",
        "|---|---|---|---|",
    ]
    for row in hold[:40]:
        reasons = []
        if row["real_junk"]:
            reasons.append("碎片/超长")
        if row["big_shorten"]:
            reasons.append("砍掉限定词且分型为空")
        lines.append(f"| {row['status']} | {row['generic_name']} | {row['old_short'][:40]} | {'；'.join(reasons) or '-'} |")
    if not hold:
        lines.append("| - | - | - | - |")

    lines += [
        "",
        "## 五、需要拍板",
        "",
        f"1. **大类归一**：{len(sclc)} 行小细胞/非小细胞肺癌的简称会变成“肺癌”，分型保留"
        "“小细胞肺癌 / 非小细胞肺癌 / 鳞状 / 非鳞状”。是否接受？",
        "2. **长网搜简称**：" + f"全库 {sum(1 for row in rows if row['real_junk'])} 行仍是碎片或超长（帕博利珠 4 行、度伐利尤 2 行、替雷利珠 1 行），"
        "建议 P1 用 LLM 兜底，不在这轮回填里硬改。",
        "",
        "## 六、建议",
        "",
        f"- 先只写 DRAFT 中 {len(draft_safe)} 条“有变化且可自动写”的行（另有 {len(draft_noop)} 条无变化，不用写）；",
        f"- 全库其余 {len(hold) - len(draft_hold)} 条 APPROVED 行等你看完 diff 再决定；",
        "- 碎片/长名统一交给 P1 的 LLM 归一（约 ¥0.1）。",
        "",
    ]
    return "\n".join(lines)


def main_cli() -> int:
    rows = build()
    report = render(rows)
    os.makedirs("reports", exist_ok=True)
    md_path = os.path.join("reports", "p1_backfill_preview.md")
    json_path = os.path.join("reports", "p1_backfill_preview.json")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(report)
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows},
                  handle, ensure_ascii=False, indent=2)
    draft = [row for row in rows if row["status"] == "DRAFT"]
    print(f"报告: {md_path}")
    draft_write = [row for row in draft if row["safe_auto"] and row["changed"]]
    print(f"总行 {len(rows)}，DRAFT {len(draft)}，"
          f"有变化可写 {len(draft_write)}，无变化 {sum(1 for row in draft if not row['changed'] and row['safe_auto'])}，"
          f"暂缓 {sum(1 for row in draft if not row['safe_auto'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
