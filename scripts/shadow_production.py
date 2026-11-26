"""生产路径 shadow：同一批 20 条，走 INDICATION_MODE=llm + INDICATION_DICTS=off。

做法：
1. 复制 drug_agent.db 到临时库；
2. 用副本库跑 indication_llm.normalize_products → flatten_to_rows → _save_rows；
3. 对比写库前后行数，确认没有新建重复行；
4. 不碰真实库。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IDS = [
    97, 98, 99, 100, 101,
    23, 24, 55, 83, 86, 96,
    72, 85, 93, 95, 60, 68, 81, 82, 92,
]

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
src_path = os.path.join(ROOT, "drug_agent.db")
copy_path = f"/tmp/ypcx_production_shadow_{ts}.db"
src = sqlite3.connect(src_path)
dst = sqlite3.connect(copy_path)
with dst:
    src.backup(dst)
dst.close()
src.close()

os.environ["DATABASE_URL"] = f"sqlite:///{copy_path}"
os.environ["INDICATION_MODE"] = "llm"
os.environ["INDICATION_DICTS"] = "off"
os.environ["LLM_INDICATION_THINKING"] = "0"

import indication_llm  # noqa: E402
import main  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import Drug  # noqa: E402


def build_products(records):
    groups = {}
    order = []
    for row in records:
        key = (row.generic_name, row.brand_name, row.manufacturer, row.dosage_form, row.specification)
        if key not in groups:
            groups[key] = {
                "generic_name": row.generic_name,
                "brand_name": row.brand_name,
                "manufacturer": row.manufacturer,
                "dosage_form": row.dosage_form,
                "specification": row.specification,
                "china_approval_date": row.china_approval_date,
                "origin_type": row.origin_type,
                "origin_region": row.origin_region,
                "price_market": row.price_market,
                "nhsa_pdf_url": row.nhsa_pdf_url,
                "nhsa_ppt_url": row.nhsa_ppt_url,
                "indications": [],
            }
            order.append(key)
        groups[key]["indications"].append({
            "indication_short": row.indication_short,
            "indication_subtype": row.indication_subtype,
            "indication_full": row.indication_full,
            "approval_country": row.approval_country or "中国",
            "approval_date": row.indication_approval_date,
            "guidelines": [],
        })
    return [groups[key] for key in order]


def main_cli():
    with SessionLocal() as session:
        records = session.query(Drug).filter(Drug.id.in_(IDS)).order_by(Drug.id).all()
        before = {row.id: (row.indication_short, row.indication_subtype) for row in records}
        count_before = session.query(Drug).count()
        products = build_products(records)

    stats = indication_llm.normalize_products(products, thinking=False)
    data = {"generic_name": "shadow", "products": products, "fetch_time": ts}
    rows = main.flatten_to_rows(data)
    for row in rows:
        row["status"] = "DRAFT"
    saved = main._save_rows("shadow", rows)

    with SessionLocal() as session:
        count_after = session.query(Drug).count()
        after = {row.id: (row.indication_short, row.indication_subtype) for row in session.query(Drug).filter(Drug.id.in_(IDS)).all()}

    print(f"stats={stats}")
    print(f"rows_before={count_before} rows_after={count_after} delta={count_after - count_before}")
    print(f"saved={len(saved)}")
    print("id | old | new | llm_subtype")
    for rid in IDS:
        old = before.get(rid, ("", ""))
        new = after.get(rid, ("", ""))
        print(f"{rid} | {old[0]} / {old[1]} | {new[0]} / {new[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
