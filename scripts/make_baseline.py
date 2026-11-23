"""冻结当前库输出作为基线（防止后续重构无意改变行为）。

用法：
    ./venv/bin/python scripts/make_baseline.py
    ./venv/bin/python scripts/make_baseline.py --out data/golden/baseline_generics.json

注意：基线是“当前输出”，不是“正确答案”；真值集后续由人工反馈补充。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal  # noqa: E402
from models import Drug  # noqa: E402

FIELDS = (
    "generic_name", "brand_name", "manufacturer", "origin_type", "origin_region",
    "indication_short", "indication_subtype", "indication_full", "approval_country",
    "china_approval_date", "indication_approval_date", "drug_insurance_date",
    "insurance_status_text", "dosage_form", "specification", "status",
)


def build() -> dict:
    with SessionLocal() as session:
        rows = session.query(Drug).order_by(Drug.generic_name, Drug.id).all()
    records = []
    for row in rows:
        record = {field: getattr(row, field, "") for field in FIELDS}
        record["has_official_doc"] = bool(row.nhsa_pdf_url or row.nhsa_ppt_url)
        records.append(record)
    generics = sorted({record["generic_name"] for record in records})
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "drug_agent.db",
        "note": "baseline, not ground truth",
        "generics": generics,
        "counts": {"generics": len(generics), "rows": len(records)},
        "rows": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join("data", "golden", "baseline_generics.json"))
    args = parser.parse_args()
    payload = build()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"已写入 {args.out}: {payload['counts']['generics']} 个通用名 / {payload['counts']['rows']} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
