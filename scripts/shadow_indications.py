"""20 条适应症 shadow：用 LLM（不带病名词典）重算简称/分型，只对比不写库。

用法：
    ./venv/bin/python scripts/shadow_indications.py
输出：
    reports/indication_shadow_<ts>.md / .json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indication_llm  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import Drug  # noqa: E402

# 5 条泰它西普 + 15 条已知问题行（长简称/碎片/活动性）
IDS = [
    97, 98, 99, 100, 101,
    23, 24, 55, 83, 86, 96,
    72, 85, 93, 95, 60, 68, 81, 82, 92,
]


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    with SessionLocal() as session:
        rows = session.query(Drug).filter(Drug.id.in_(IDS)).order_by(Drug.id).all()
        records = [
            {
                "id": row.id,
                "generic_name": row.generic_name,
                "brand_name": row.brand_name,
                "manufacturer": row.manufacturer,
                "dosage_form": row.dosage_form,
                "specification": row.specification,
                "old_short": row.indication_short,
                "old_subtype": row.indication_subtype,
                "full": row.indication_full,
                "status": row.status,
            }
            for row in rows
        ]
    payload = [{"id": r["id"], "generic_name": r["generic_name"], "full": r["full"]} for r in records]

    # 20 条一次调用容易超时，按 5 条一批跑
    normalized = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0}
    total_cost = 0.0
    total_latency = 0
    for offset in range(0, len(payload), 5):
        chunk = payload[offset:offset + 5]
        print(f"batch {offset // 5 + 1}/{(len(payload) + 4) // 5} ids={[r['id'] for r in chunk]}", flush=True)
        part, result = indication_llm.normalize(chunk, model="qwen3.8-flash", thinking=False, timeout=60)
        if not part:
            print("  batch failed", flush=True)
        normalized.update(part)
        if result:
            for key in usage:
                usage[key] += int(result.usage.get(key, 0) or 0)
            total_cost += float(result.cost_cny or 0.0)
            total_latency += int(result.latency_ms or 0)
    missing = [row for row in payload if row["id"] not in normalized]
    if missing:
        print(f"retry missing ids={[r['id'] for r in missing]}", flush=True)
        part, result = indication_llm.normalize(missing, model="qwen3.8-flash", thinking=False, timeout=60)
        normalized.update(part)
        if result:
            for key in usage:
                usage[key] += int(result.usage.get(key, 0) or 0)
            total_cost += float(result.cost_cny or 0.0)
            total_latency += int(result.latency_ms or 0)
    changes = []
    for record in records:
        item = normalized.get(record["id"])
        changes.append({
            **record,
            "new_short": item.short if item else "",
            "new_subtype": item.subtype if item else "",
            "disease": item.disease if item else "",
            "has_subtypes": item.has_subtypes if item else None,
            "evidence": item.evidence if item else "",
            "ok": bool(item),
        })

    matched = 0
    with SessionLocal() as session:
        for record in records:
            exists = session.query(Drug).filter(
                Drug.generic_name == record["generic_name"],
                Drug.brand_name == record["brand_name"],
                Drug.manufacturer == record["manufacturer"],
                Drug.dosage_form == record["dosage_form"],
                Drug.specification == record["specification"],
                Drug.indication_full == record["full"],
            ).count()
            if exists:
                matched += 1

    os.makedirs("reports", exist_ok=True)
    md_path = os.path.join("reports", f"indication_shadow_{ts}.md")
    json_path = os.path.join("reports", f"indication_shadow_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"changes": changes, "usage": usage, "cost_cny": round(total_cost, 6),
                   "latency_ms": total_latency}, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(f"# 适应症 LLM shadow（{ts}）\n\n")
        handle.write(f"- 样本：{len(records)} 行\n- LLM：qwen3.8-flash 非思考（5 条/批，无病名词典）\n")
        handle.write(f"- tokens：{usage}，费用 ¥{round(total_cost, 6)}，耗时 {total_latency}ms\n")
        handle.write(f"- 可按“通用名+厂家+剂型+规格+全文”匹配的已有行：{matched}/{len(records)}（写库会更新而不是新建）\n\n")
        handle.write("| id | 药品 | 旧简称 | 新简称 | 旧分型 | 新分型 | 结果 |\n|---|---|---|---|---|---|---|\n")
        for change in changes:
            handle.write(
                f"| {change['id']} | {change['generic_name']} | {change['old_short']} | {change['new_short']} "
                f"| {change['old_subtype'] or '-'} | {change['new_subtype'] or '-'} | {'OK' if change['ok'] else '模型未返回/校验失败'} |\n"
            )
        handle.write("\n## 明细\n\n")
        for change in changes:
            handle.write(f"### {change['id']} {change['generic_name']}\n\n")
            handle.write(f"- 全文：{change['full']}\n")
            handle.write(f"- disease：{change['disease']}；has_subtypes：{change['has_subtypes']}\n")
            handle.write(f"- evidence：{change['evidence']}\n\n")
    print(f"report: {md_path}")
    print(f"ok {sum(1 for c in changes if c['ok'])}/{len(changes)}, matched {matched}/{len(changes)}, cost {round(total_cost, 6)}")
    for change in changes:
        if change["ok"] and (change["old_short"] != change["new_short"] or change["old_subtype"] != change["new_subtype"]):
            print(f"  {change['id']} {change['old_short']} -> {change['new_short']} / {change['new_subtype']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
