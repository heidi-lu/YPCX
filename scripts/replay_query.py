"""离线回放一次查询：只读快照，不联网、不调用任何付费接口。

前置：先用正常流程跑过该药（会写 snapshots/）。缺失快照时会明确报错。

用法：
    ./venv/bin/python scripts/replay_query.py 替雷利珠单抗
    ./venv/bin/python scripts/replay_query.py 替雷利珠单抗 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import agent  # noqa: E402
import llm_client  # noqa: E402
import main  # noqa: E402
import source_snapshot  # noqa: E402


def replay(drug: str) -> dict:
    files = source_snapshot.load_files(drug)
    if files is None:
        raise SystemExit(f"没有 {drug} 的文件列表快照，请先正常跑一次该药。")

    missing_docs = []
    real_get = requests.get

    def fake_get(url, **kwargs):
        content = source_snapshot.load_doc(url)
        if content is None:
            missing_docs.append(url)
            raise RuntimeError(f"缺少文档快照: {url}")
        return SimpleNamespace(content=content, status_code=200, url=url)

    def fake_search(query, max_results=5, **kwargs):
        results = source_snapshot.load_search(query)
        if results is None:
            return []
        return results

    # 回放只验证“正则 + 组装”链路，VLM 用空结果代替（不再付费）。
    real_vision = agent.extract_medical_pdf_vision
    real_scheduler_fetch = agent.scheduler.get_or_fetch
    real_chat = llm_client.chat
    try:
        requests.get = fake_get
        agent.ds_search = fake_search
        agent.extract_medical_pdf_vision = lambda pdf_bytes, ppt_bytes=None: {}
        agent.scheduler.get_or_fetch = lambda field, name, loader: files
        # 回放不调用模型：返回 None 让上层走规则/跳过，保证 0 费用。
        llm_client.chat = lambda *args, **kwargs: None
        agent.usage_store.set_drug(drug)
        data = agent.fetch_new_drug(drug)
    finally:
        requests.get = real_get
        agent.extract_medical_pdf_vision = real_vision
        agent.scheduler.get_or_fetch = real_scheduler_fetch
        llm_client.chat = real_chat

    rows = main.flatten_to_rows(data)
    return {
        "drug": drug,
        "products": len(data.get("products") or []),
        "rows": len(rows),
        "missing_docs": missing_docs,
        "data": data,
        "rows_data": rows,
    }


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("drug")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    result = replay(args.drug)
    print(f"{args.drug}: products={result['products']} rows={result['rows']}")
    for row in result["rows_data"]:
        print(f"  - {row.get('indication_short')} / {row.get('indication_subtype') or '-'} "
              f"/ {row.get('indication_approval_date') or '空'} / {row.get('approval_country')}")
    if result["missing_docs"]:
        print(f"⚠️ 缺失 {len(result['missing_docs'])} 个文档快照（VLM 已跳过）")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        print(f"已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
