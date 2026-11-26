"""把 LLM 归一结果写回 drugs.indication_short / indication_subtype。

默认 dry-run，只出 before/after；加 --apply 才写库，且必须显式设置
LLM_OVERWRITE_APPROVED=1（防止误覆盖已入库数据）。

用法：
    LLM_OVERWRITE_APPROVED=1 ./venv/bin/python scripts/apply_llm_indications.py            # dry-run
    LLM_OVERWRITE_APPROVED=1 ./venv/bin/python scripts/apply_llm_indications.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import indication_llm  # noqa: E402

IDS = [97, 98, 99, 100, 101, 23, 24, 55, 83, 86, 96, 72, 85, 93, 95, 60, 68, 81, 82, 92]


def load_rows(db_path: str, all_rows: bool = False) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if all_rows:
        rows = con.execute(
            "select id,generic_name,status,indication_short,indication_subtype,indication_full "
            "from drugs where indication_full != '' order by id"
        ).fetchall()
    else:
        rows = con.execute(
            f"select id,generic_name,status,indication_short,indication_subtype,indication_full "
            f"from drugs where id in ({','.join('?' * len(IDS))}) order by id", IDS
        ).fetchall()
    con.close()
    return [dict(row) for row in rows]


def backup(db_path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(os.path.dirname(db_path), f"drug_agent.backup_{ts}_before_llm_indications.db")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true", help="全库回填（默认只跑固定 20 条）")
    parser.add_argument("--db", default=os.path.join(ROOT, "drug_agent.db"))
    args = parser.parse_args()

    rows = load_rows(args.db, all_rows=args.all)
    payload = [{"id": row["id"], "generic_name": row["generic_name"], "full": row["indication_full"]} for row in rows]
    normalized = {}
    cost = 0.0
    for offset in range(0, len(payload), 5):
        part, result = indication_llm.normalize(payload[offset:offset + 5], model="qwen3.8-flash", thinking=False)
        normalized.update(part)
        if result:
            cost += float(result.cost_cny or 0.0)

    changes = []
    for row in rows:
        item = normalized.get(row["id"])
        if not item:
            continue
        old = (row["indication_short"] or "", row["indication_subtype"] or "")
        new = (item.short, item.subtype)
        if old == new:
            continue
        changes.append({
            "id": row["id"], "generic_name": row["generic_name"], "status": row["status"],
            "old_short": old[0], "new_short": new[0],
            "old_subtype": old[1], "new_subtype": new[1],
        })

    print(f"matched {len(normalized)}/{len(rows)}, changed {len(changes)}, llm_cost ¥{round(cost, 6)}")
    print("id | status | old_short -> new_short | old_subtype -> new_subtype")
    for change in changes:
        print(f"{change['id']} | {change['status']} | {change['old_short']} -> {change['new_short']} "
              f"| {change['old_subtype'] or '-'} -> {change['new_subtype'] or '-'}")

    if not args.apply:
        print("dry-run：未写库。加 --apply 且设置 LLM_OVERWRITE_APPROVED=1 才写。")
        return 0

    if os.environ.get("LLM_OVERWRITE_APPROVED") != "1":
        print("拒绝写库：请显式设置 LLM_OVERWRITE_APPROVED=1")
        return 2
    if not changes:
        print("没有需要变更的行。")
        return 0

    backup_path = backup(args.db)
    con = sqlite3.connect(args.db)
    try:
        with con:
            for change in changes:
                cur = con.execute(
                    "update drugs set indication_short=?, indication_subtype=? where id=?",
                    (change["new_short"], change["new_subtype"], change["id"]),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"update id={change['id']} rowcount={cur.rowcount}")
    finally:
        con.close()

    print(f"已写库：{len(changes)} 行；备份 {backup_path}")
    report = os.path.join(ROOT, "reports", f"llm_indications_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as handle:
        json.dump({"backup": backup_path, "changes": changes, "cost_cny": round(cost, 6)},
                  handle, ensure_ascii=False, indent=2)
    print(f"报告 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
