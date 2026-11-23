"""Offline smoke tests. No LLM, network, or live GitHub calls."""

import pytest
from fastapi.testclient import TestClient

import main
from agent import (
    _clean_brand_name,
    _is_chinese_brand,
    _regex_dosage_fields,
    _split_numbered_indications,
    _strength_from_text,
)
from build_dict import normalize_execution_date, parse_execution
from models import Drug


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_drugs_endpoint(client):
    response = client.get("/drugs")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_normalize_dosage_form():
    assert main._normalize_dosage_form("注射液") == "注射剂"
    assert main._normalize_dosage_form("片剂") == "片剂"
    assert main._normalize_dosage_form("胶囊") == "胶囊"
    assert main._normalize_dosage_form("装置") == "植入剂"
    assert main._normalize_dosage_form("") == ""


def test_match_nhi_exact(monkeypatch):
    monkeypatch.setattr(
        main,
        "NHI_DICT",
        {"测试药|片剂": {"first_year": "2020", "restriction_timeline": {}}},
    )
    assert main._match_nhi("测试药", "片剂")["first_year"] == "2020"


def test_derive_insurance_year():
    nhi = {
        "restriction_timeline": {
            "2022": "限测试适应症",
            "2023": "无限制",
        }
    }
    assert main._derive_insurance_year(nhi, "测试适应症", "2020年") == "2022"


def test_flatten_keeps_dosage():
    data = {
        "generic_name": "PYTEST_UNIQUE_DRUG",
        "drug_type": "测试类型",
        "fetch_time": "2026-11-21",
        "products": [
            {
                "generic_name": "PYTEST_UNIQUE_DRUG",
                "brand_name": "测试品牌",
                "manufacturer": "测试厂家",
                "origin_type": "仿制",
                "dosage_form": "片剂",
                "china_approval_date": "2020年1月",
                "indications": [
                    {
                        "indication_short": "测试适应症",
                        "approval_date": "2020年1月",
                        "indication_full": "测试适应症说明书",
                        "dosage": "每次1片，每日一次",
                    }
                ],
            }
        ],
    }
    rows = main.flatten_to_rows(data)
    assert len(rows) == 1
    assert rows[0]["indication_short"] == "测试适应症"
    assert rows[0]["dosage"] == "每次1片，每日一次"
    assert rows[0]["indication_approval_date"] == "2020年1月"


def test_ppt_brand_skips_reference_drug():
    from skill_vision_parser import _extract_brand_from_text

    text = (
        "通用名/商品名菲诺利单抗注射液/安佑平英文名Finotonlimab注册规格100mg"
        "参照药品建议：西妥昔单抗（爱必妥®）"
    )
    assert _extract_brand_from_text(text) == "安佑平"

    text2 = "商品名：安佑平；参照药品：西妥昔单抗（爱必妥®）"
    assert _extract_brand_from_text(text2) == "安佑平"


def test_abbreviations_come_from_disease_dictionary():
    import json

    from disease_terms import ABBREVIATIONS

    raw = json.load(open("medical_aliases.json", encoding="utf-8"))
    assert raw["非小细胞肺癌"]["abbreviations"] == ["NSCLC"]
    assert ABBREVIATIONS["NSCLC"] == "非小细胞肺癌"
    assert ABBREVIATIONS["COPD"] == "慢性阻塞性肺疾病"
    assert ABBREVIATIONS["gMG"] == "重症肌无力"


def test_disease_coverage_keeps_sclc_and_nsclc_separate():
    from agent import _disease_covered

    assert not _disease_covered("小细胞肺癌", [{"indication_full": "非小细胞肺癌"}])
    assert _disease_covered("非小细胞肺癌", [{"indication_full": "非小细胞肺癌"}])


def test_nhsa_name_variants_collapse_repeated_antibody_suffix():
    from nhsa_crawler import _name_variants

    variants = _name_variants("艾帕洛利单抗托沃瑞利单抗")
    assert "艾帕洛利托沃瑞利单抗" in variants


def test_execution_date_preferred_over_year_column():
    row = ["2022", "2023年3月1日"]
    col_idx = {"year_direct": 0, "exec_time": 1}
    assert parse_execution(row, col_idx) == ("2023", "2023年3月1日", "执行时间")
    assert normalize_execution_date("2023-03-01") == "2023年3月1日"


def test_alias_matches_old_name():
    entry = main._match_nhi("多拉司琼", "注射剂")
    assert entry is not None
    assert entry["generic_name"] == "甲磺酸多拉司琼注射液"
    assert entry["aliases"]


def test_alias_key_matches_form_variant():
    entry = main._match_nhi("丁细牙痛胶囊", "胶囊")
    assert entry is not None
    assert entry["generic_name"] == "丁细牙痛胶囊"
    assert entry["dosage_form"] == "胶囊剂"


def test_split_metadata_and_parenthesis_merge():
    composite = main._match_nhi("脑血康丸（片胶囊颗粒口服液滴丸）", "口服液")
    assert composite is not None
    assert "脑血康丸" in composite["split_to"]

    merged = main._match_nhi("脑血康片（滴丸）", "滴丸")
    assert merged is not None
    assert merged["dosage_form"] == "滴丸"
    assert "2021" in merged["restriction_timeline"]
    assert "2026" in merged["restriction_timeline"]


def test_flatten_returns_execution_date():
    data = {
        "generic_name": "甲磺酸多拉司琼注射液",
        "drug_type": "测试类型",
        "products": [{
            "generic_name": "甲磺酸多拉司琼注射液",
            "brand_name": "",
            "manufacturer": "",
            "origin_type": "原研",
            "dosage_form": "注射剂",
            "china_approval_date": "2020年",
            "indications": [{"indication_short": "放化疗且吞咽困难患者", "approval_date": "2020年"}],
        }],
    }
    rows = main.flatten_to_rows(data)
    assert rows
    assert rows[0]["drug_insurance_date"] == "2017年"
    # 医保目录优先，不因获批时间去否定目录年份
    assert rows[0]["indication_insurance_date"] == "2017年"


def test_asthma_display_keeps_subtype():
    display, subtype = main._display_indication("重度嗜酸粒细胞性哮喘")
    assert display == "哮喘"
    assert subtype == "重度嗜酸粒细胞性哮喘"

    display, subtype = main._display_indication("过敏性哮喘")
    assert display == "哮喘"
    assert subtype == "过敏性哮喘"


def test_indication_alias_matching():
    mepo = main._match_nhi("美泊利珠单抗注射液", "注射剂")
    assert main._derive_insurance_year(mepo, "COPD维持治疗", "2025-12") == "2027年1月1日"
    assert main._derive_insurance_year(mepo, "SEA维持治疗", "") == "2025年1月1日"
    assert main._derive_insurance_year(mepo, "EGPA维持治疗", "") == "2025年1月1日"

    beli = main._match_nhi("注射用贝利尤单抗", "注射剂")
    assert main._derive_insurance_year(beli, "狼疮性肾炎", "2024-03") == "2024年1月1日"


def test_multi_subtype_uses_detail_in_short():
    data = {
        "generic_name": "测试哮喘药",
        "products": [{
            "generic_name": "测试哮喘药",
            "origin_type": "原研",
            "dosage_form": "注射剂",
            "indications": [
                {"indication_short": "过敏性哮喘", "indication_full": "过敏性哮喘"},
                {"indication_short": "重度嗜酸粒细胞性哮喘", "indication_full": "重度嗜酸粒细胞性哮喘"},
            ],
        }],
    }
    rows = main.flatten_to_rows(data)
    # 简称统一最简大类，细分放“适应症分型”
    assert {r["indication_short"] for r in rows} == {"哮喘"}
    assert {r["indication_subtype"] for r in rows} == {"过敏性哮喘", "重度嗜酸粒细胞性哮喘"}

    data["products"][0]["indications"] = [{"indication_short": "过敏性哮喘", "indication_full": "过敏性哮喘"}]
    rows = main.flatten_to_rows(data)
    assert rows[0]["indication_short"] == "哮喘"
    assert rows[0]["indication_subtype"] == "过敏性哮喘"


def test_row_level_specification_and_brand_precedence():
    data = {
        "generic_name": "测试规格药",
        "products": [{
            "generic_name": "测试规格药",
            "brand_name": "原商品名",
            "origin_type": "原研",
            "dosage_form": "装置",
            "specification": "10.8mg, 3.6mg",
            "indications": [
                {"indication_short": "前列腺癌", "specification": "10.8mg", "brand_name": "别家商品名"},
                {"indication_short": "乳腺癌", "specification": "3.6mg"},
            ],
        }],
    }
    rows = main.flatten_to_rows(data)
    assert [r["specification"] for r in rows] == ["10.8mg", "3.6mg"]
    assert all(r["brand_name"] == "原商品名" for r in rows)


def test_regex_dosage_fields():
    parsed = _regex_dosage_fields("本品采用静脉输注的方式给药，推荐剂量为200mg，每3周给药1次。")
    assert parsed["dosage_single"] == "200mg"
    assert parsed["frequency"] == "每3周给药1次"
    assert parsed["route"] == "静脉输注"


def test_insurance_status_entry_and_exit():
    nhi = {
        "restriction_timeline": {
            "2021": "经典型霍奇金淋巴瘤；尿路上皮癌",
            "2022": "经典型霍奇金淋巴瘤；尿路上皮癌；鳞状非小细胞肺癌一线",
            "2023": "经典型霍奇金淋巴瘤；尿路上皮癌；鳞状非小细胞肺癌一线",
            "2024": "经典型霍奇金淋巴瘤；尿路上皮癌；鳞状非小细胞肺癌一线",
            "2025": "经典型霍奇金淋巴瘤；尿路上皮癌；鳞状非小细胞肺癌一线",
            "2026": "尿路上皮癌；鳞状非小细胞肺癌一线",
        }
    }
    assert main._insurance_status_text(nhi, "经典型霍奇金淋巴瘤", "霍奇金淋巴瘤") == "2021年进入，2026年退出"
    assert main._insurance_status_text(nhi, "尿路上皮癌", "尿路上皮癌") == "2021年进入"


def test_numbered_indications_split_rules():
    combined = "【1】经典型霍奇金淋巴瘤 本品适用于... 【2】非鳞状非小细胞肺癌（1）本品联合...（2）本品联合..."
    rows = _split_numbered_indications([{
        "indication_short": "经典型霍奇金淋巴瘤",
        "indication_full": combined,
        "approval_date": "2026-05",
        "approval_country": "中国",
    }])
    assert [r["indication_short"] for r in rows] == ["经典型霍奇金淋巴瘤", "非鳞状非小细胞肺癌"]

    paren_only = "非鳞状非小细胞肺癌（1）本品联合...（2）本品联合..."
    rows = _split_numbered_indications([{"indication_short": "非鳞状非小细胞肺癌", "indication_full": paren_only}])
    assert len(rows) == 1

    line_numbered = """1、经典型霍奇金淋巴瘤 本品...
2、非鳞状非小细胞肺癌 本品..."""
    rows = _split_numbered_indications([{"indication_short": "x", "indication_full": line_numbered}])
    assert [r["indication_short"] for r in rows] == ["经典型霍奇金淋巴瘤", "非鳞状非小细胞肺癌"]


def test_strength_from_text():
    assert _strength_from_text("醋酸戈舍瑞林缓释植入剂10.8mg【适应症】前列腺癌") == "10.8mg"
    assert _strength_from_text("3.6mg 前列腺癌；乳腺癌；子宫内膜异位症") == "3.6mg"
    assert _strength_from_text("10.8mg 和 3.6mg 两种规格") == ""


def test_chinese_brand_filter_and_no_override():
    assert _is_chinese_brand("诺和盈")
    assert not _is_chinese_brand("Wegovy")
    assert _clean_brand_name("Wegovy") == "Wegovy"
    assert main._resolve_brand("诺和盈", "诺和泰") == "诺和盈"
    assert main._resolve_brand("茁乐", "蜀阳舒乐") == "茁乐"


def test_manufacturer_truncated_suffix():
    assert main.normalize_manufacturer("泰州迈博太科有限公") == "泰州迈博太科"
    assert main.normalize_manufacturer("泰州迈博太科有限公司") == "泰州迈博太科"
    assert main.normalize_manufacturer("上市许可持有人（授权企业）") == ""


def test_drug_to_dict_strips_control_characters():
    drug = Drug(generic_name="测试药", brand_name="品\x00牌")
    result = main._drug_to_dict(drug)
    assert result["brand_name"] == "品牌"


def test_short_name_cleanup_strips_trailing_colon():
    from agent import _regex_short_cleanup

    products = [{"indications": [
        {"indication_full": "【4】食管鳞状细胞癌：本品联合含氟尿嘧啶类和铂类药物用于一线治疗。", "indication_short": "x"},
        {"indication_full": "宫颈癌:本品单药用于既往治疗失败的患者", "indication_short": "宫颈癌:"},
    ]}]
    _regex_short_cleanup(products)
    assert [i["indication_short"] for i in products[0]["indications"]] == ["食管癌", "宫颈癌"]


def test_indication_regex_short_strips_colon():
    from indication_regex import _short

    assert _short("宫颈癌:本品单药用于既往接受含铂化疗治疗失败的复发或转移性宫颈癌患者的治疗") == "宫颈癌"
    assert _short("【2】小细胞肺癌：本品联合卡铂和依托泊苷用于一线治疗。") == "小细胞肺癌"


def test_display_indication_keeps_lung_subtypes():
    assert main._display_indication("非小细胞肺癌") == ("非小细胞肺癌", "")
    assert main._display_indication("小细胞肺癌") == ("小细胞肺癌", "")


def test_insurance_status_covers_non_hardcoded_diseases():
    cervical = {"restriction_timeline": {
        "2025": "既往接受含铂化疗治疗失败的复发或转移性宫颈癌患者的治疗。",
        "2026": "1.既往接受含铂化疗治疗失败的复发或转移性宫颈癌患者的治疗；2.联合紫杉醇和铂类用于持续、复发或转移性宫颈癌的一线治疗。",
    }}
    assert main._insurance_status_text(cervical, "宫颈癌:本品单药用于既往治疗失败", "宫颈癌") == "2025年进入"
    assert main._insurance_status_text(
        cervical, "本品联合紫杉醇和铂类用于持续、复发或转移性宫颈癌的一线治疗", "宫颈癌"
    ) == "2026年进入"

    atopic = {"restriction_timeline": {"2021": "限对传统治疗无效的中重度特应性皮炎患者"}}
    assert main._insurance_status_text(atopic, "特应性皮炎", "特应性皮炎") == "2021年进入"

    nmo = {"restriction_timeline": {"2023": "限抗水通道蛋白4抗体阳性的视神经脊髓炎谱系疾病成人患者"}}
    assert main._insurance_status_text(nmo, "视神经脊髓炎谱系疾病", "视神经脊髓炎") == "2023年进入"


def test_country_dedupe_merges_alias_and_drops_web_duplicate():
    rows = [
        {"generic_name": "度普利尤单抗注射液", "manufacturer": "赛诺菲", "indication_short": "鼻窦炎",
         "indication_full": "慢性鼻窦炎伴鼻息肉", "approval_country": "美国"},
        {"generic_name": "度普利尤单抗注射液", "manufacturer": "赛诺菲", "indication_short": "慢性鼻窦炎伴鼻息肉",
         "indication_full": "慢性鼻窦炎伴鼻息肉（CRSwNP）成人患者", "approval_country": "美国"},
    ]
    assert len(main._country_dedupe_rows(rows)) == 1

    official = {
        "generic_name": "伊奈利珠单抗注射液", "manufacturer": "常州恒邦",
        "indication_short": "视神经脊髓炎",
        "indication_full": "本品适用于抗水通道蛋白4（AQP4）抗体阳性的视神经脊髓炎谱系疾病（NMOSD）成人患者的治疗",
        "approval_country": "中国", "nhsa_pdf_url": "http://example.com/a.pdf",
    }
    phantom = {
        "generic_name": "伊奈利珠单抗", "manufacturer": "江苏豪森", "indication_short": "NMOSD",
        "indication_full": "适用于抗水通道蛋白4（AQP4）抗体阳性的视神经脊髓炎谱系疾病（NMOSD）成人患者的治疗",
        "approval_country": "中国", "nhsa_pdf_url": "",
    }
    assert len(main._country_dedupe_rows([official, phantom])) == 1


def test_inherit_guidelines_filters_by_disease_and_line():
    from agent import _inherit_pdf_indication_fields

    rows = [
        {"indication_full": "1.经典型霍奇金淋巴瘤的治疗。"},
        {"indication_full": "3.鳞状非小细胞肺癌的一线治疗。"},
        {"indication_full": "6.非小细胞肺癌新辅助及辅助治疗。"},
        {"indication_full": "7.广泛期小细胞肺癌的一线治疗。"},
    ]
    original = [{
        "indication_short": "非小细胞肺癌",
        "guidelines": [
            {"name": "《CSCO非小细胞肺癌诊疗指南》", "recommendation": "推荐用于围术期治疗（新辅助+辅助治疗）"},
        ],
    }, {
        "indication_short": "小细胞肺癌",
        "guidelines": [
            {"name": "《CSCO小细胞肺癌诊疗指南》", "recommendation": "广泛期小细胞肺癌一线治疗"},
        ],
    }]
    _inherit_pdf_indication_fields(rows, original)

    assert "guidelines" not in rows[0]
    assert [g["name"] for g in rows[1]["guidelines"]] == ["《CSCO非小细胞肺癌诊疗指南》"]
    assert rows[1]["guidelines"][0]["recommendation"] == ""
    assert rows[2]["guidelines"][0]["recommendation"].startswith("推荐用于围术期")
    assert [g["name"] for g in rows[3]["guidelines"]] == ["《CSCO小细胞肺癌诊疗指南》"]


def test_pdf_cover_manufacturer_joins_wrapped_lines():
    import pymupdf
    from skill_vision_parser import _extract_pdf_key_info

    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(["企业名称 ：", "上海复宏瑞霖生物技术有", "限公司"]):
        page.insert_text((72, 72 + i * 18), line, fontname="china-s", fontsize=11)
    res = _extract_pdf_key_info(doc.tobytes())
    assert res["manufacturer"] == "上海复宏瑞霖生物技术有限公司"


def test_normalize_disease_keeps_lung_subtypes():
    from agent import _normalize_disease_with_dict

    assert _normalize_disease_with_dict("非小细胞肺癌") == "非小细胞肺癌"
    assert _normalize_disease_with_dict("非鳞状非小细胞肺癌") == "非小细胞肺癌"
    assert _normalize_disease_with_dict("小细胞肺癌") == "小细胞肺癌"
    assert _normalize_disease_with_dict("肺癌") == "肺癌"


def test_disease_mentioned_expands_common_abbreviations():
    from agent import _disease_mentioned

    assert _disease_mentioned("中重度慢阻肺患者", "慢性阻塞性肺疾病")
    assert not _disease_mentioned("特应性皮炎", "慢性阻塞性肺疾病")
    assert _disease_mentioned("CSCO NSCLC 指南", "非小细胞肺癌")
    assert not _disease_mentioned("非小细胞肺癌", "小细胞肺癌")


def test_collect_ppt_text_reads_tables():
    import io
    from pptx import Presentation
    from pptx.util import Inches
    from skill_vision_parser import _collect_ppt_text

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "卡度尼利单抗获多部宫颈癌指南推荐"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    table.cell(0, 0).text = "CSCO宫颈癌诊疗指南（2024）"
    table.cell(1, 0).text = "卡度尼利单抗联合化疗±贝伐（1类）"
    buf = io.BytesIO()
    prs.save(buf)

    text = _collect_ppt_text(buf.getvalue())
    assert "CSCO宫颈癌诊疗指南（2024）" in text
    assert "卡度尼利单抗联合化疗±贝伐（1类）" in text


def test_extract_ppt_info_reads_guidelines(monkeypatch):
    import io, json
    from types import SimpleNamespace
    from pptx import Presentation
    import skill_vision_parser as svp

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "卡度尼利单抗获指南推荐"
    buf = io.BytesIO()
    prs.save(buf)

    payload = {
        "indication_approval_times": [],
        "guidelines": [{
            "indication_short": "宫颈癌",
            "name": "CSCO宫颈癌诊疗指南（2024）",
            "recommendation": "卡度尼利单抗联合化疗±贝伐（1类）",
            "level": "1类",
        }],
    }
    fake = SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(svp.llm_client, "chat", lambda *a, **k: fake)

    res = svp._extract_ppt_info(buf.getvalue())
    assert res["guidelines"][0]["name"] == "CSCO宫颈癌诊疗指南（2024）"
    assert res["guidelines"][0]["indication_short"] == "宫颈癌"


def test_attach_ppt_guidelines_matches_by_disease():
    from agent import _attach_ppt_guidelines

    indications = [{
        "indication_short": "宫颈癌",
        "indication_full": "本品联合紫杉醇和铂类化疗药物用于持续、复发或转移性宫颈癌的一线治疗",
    }]
    ppt_guidelines = [
        {"indication_short": "宫颈癌", "name": "CSCO宫颈癌诊疗指南（2024）",
         "recommendation": "卡度尼利单抗联合化疗±贝伐（I级推荐）", "level": "1类"},
        {"indication_short": "胃癌", "name": "CSCO胃癌诊疗指南（2025）",
         "recommendation": "卡度尼利单抗联合XELOX（1A类）", "level": "1A类"},
    ]
    _attach_ppt_guidelines(ppt_guidelines, indications)
    assert [g["name"] for g in indications[0]["guidelines"]] == ["CSCO宫颈癌诊疗指南（2024）"]


def test_filter_recommendation_keeps_matching_treatment_line():
    from agent import _filter_recommendation

    rec = ("持续、复发或转移性子宫颈癌的一线治疗：卡度尼利单抗联合化疗±贝伐（1类）；"
           "复发、转移性子宫颈癌的二线治疗：卡度尼利单抗（2A类）")
    first_line = "本品联合紫杉醇和铂类化疗药物用于持续、复发或转移性宫颈癌的一线治疗"
    second_line = "本品单药用于既往接受含铂化疗治疗失败的复发或转移性宫颈癌患者的治疗"
    assert "一线治疗" in _filter_recommendation(rec, first_line)
    assert "二线治疗" not in _filter_recommendation(rec, first_line)
    assert "二线治疗" in _filter_recommendation(rec, second_line)
    assert "卡度尼利单抗联合化疗±贝伐" not in _filter_recommendation(rec, second_line)


def test_judge_origin_region():
    from agent import _judge_origin_region

    assert _judge_origin_region("中国", "", "江苏恒瑞医药") == "国产"
    assert _judge_origin_region("美国", "", "赛诺菲") == "进口"
    assert _judge_origin_region("", "国药准字S20233106", "") == "国产"
    assert _judge_origin_region("", "", "赛诺菲") == "进口"
    assert _judge_origin_region("", "", "江苏恒瑞医药") == "国产"


def test_enrich_market_price(monkeypatch):
    import agent

    monkeypatch.setattr(agent, "ds_search", lambda *a, **k: [
        {"title": "阿得贝利单抗", "url": "https://www.315jiage.cn/n483007", "content": "最低销售价格：￥9477.00"}
    ])
    monkeypatch.setattr(agent, "_call_llm", lambda prompt, cls, **kwargs: cls(
        price_market="￥9477.00/盒（600mg）", price_date="2026-11",
        source="https://www.315jiage.cn/n483007",
    ))
    products = [{"generic_name": "阿得贝利单抗注射液", "brand_name": "艾瑞利", "price_market": ""}]
    agent._enrich_market_price(products)
    assert products[0]["price_market"].startswith("￥9477.00")
    assert "兔灵" in products[0]["price_market"]


def test_flatten_carries_origin_region_and_prices(monkeypatch):
    monkeypatch.setattr(main, "_match_nhi", lambda *a, **k: {
        "price_medical_insurance": "52.60元(10mg/瓶)",
        "catalog_type": "基本医保",
        "insurance_category": "乙",
        "restriction_timeline": {},
    })
    data = {
        "generic_name": "测试药",
        "products": [{
            "generic_name": "测试药", "brand_name": "品牌", "manufacturer": "厂家",
            "origin_type": "原研", "origin_region": "国产",
            "price_market": "￥100.00/盒（兔灵 2026-11）",
            "indications": [{"indication_short": "适应症A", "indication_full": "适应症A", "approval_country": "中国"}],
        }],
    }
    rows = main.flatten_to_rows(data)
    assert rows[0]["origin_region"] == "国产"
    assert rows[0]["price_insurance"].startswith("52.60")
    assert "兔灵" in rows[0]["price_market"]


def test_approval_persists_in_db(client):
    from db import SessionLocal
    from models import Drug

    payload = {
        "generic_name": "PYTEST_APPROVE_DRUG",
        "brand_name": "测试品牌",
        "manufacturer": "测试厂家",
        "indication_short": "测试适应症",
        "indication_full": "测试适应症",
        "status": "DRAFT",
    }
    try:
        res = client.post("/drugs", json=payload)
        assert res.status_code == 200
        assert res.json()["status"] == "APPROVED"
        approved = client.get("/drugs").json()
        assert any(r["generic_name"] == "PYTEST_APPROVE_DRUG" for r in approved)
    finally:
        with SessionLocal() as s:
            s.query(Drug).filter(Drug.generic_name == "PYTEST_APPROVE_DRUG").delete()
            s.commit()


def test_clamp_indication_approval_date():
    # 方案 A：早于首次上市说明抽错了，留空待核实，不再用上市时间兜底
    assert main._clamp_indication_approval_date("2021-11", "2022-06", "中国") == ""
    assert main._clamp_indication_approval_date("2023-01", "2022-06", "中国") == "2023-01"
    assert main._clamp_indication_approval_date("2014-12", "2018-06", "美国") == "2014-12"
    assert main._clamp_indication_approval_date("", "2022-06", "中国") == ""


def test_insurance_status_distinguishes_squamous_and_her2():
    nhi = {"restriction_timeline": {
        "2021": "联合培美曲塞和卡铂用于非鳞状非小细胞肺癌的一线治疗",
        "2023": "局部晚期或转移性鳞状非小细胞肺癌的一线治疗",
    }}
    # 鳞状一线不应被 2021 的非鳞条款吸走
    assert main._insurance_status_text(
        nhi, "本品联合紫杉醇和卡铂用于局部晚期或转移性鳞状非小细胞肺癌患者的一线治疗", "非小细胞肺癌"
    ) == "2023年进入"

    uro = {"restriction_timeline": {
        "2022": "含铂化疗失败包括新辅助或辅助化疗12个月内进展的局部晚期或转移性尿路上皮癌的治疗",
        "2027": "联合维迪西妥单抗，用于治疗HER2表达的局部晚期或转移性尿路上皮癌",
    }}
    assert main._insurance_status_text(
        uro, "联合维迪西妥单抗，用于治疗HER2表达的局部晚期或转移性尿路上皮癌", "尿路上皮癌"
    ) == "2027年进入"
    # 老的尿路上皮癌仍然从 2022 开始
    assert main._insurance_status_text(
        uro, "含铂化疗失败包括新辅助或辅助化疗12个月内进展的局部晚期或转移性尿路上皮癌的治疗", "尿路上皮癌"
    ) == "2022年进入"


def test_validate_date_normalizes_and_drops_future():
    from agent import _validate_date

    assert _validate_date("2026年11月18日") == "2026-11-18"
    assert _validate_date("2024-10") == "2024-10"
    assert _validate_date("2024/3/5") == "2024-03-05"
    assert _validate_date("2027-06") == ""
    assert _validate_date("2024-13") == ""
    assert _validate_date("") == ""


def test_declaration_conditions_do_not_eat_page_break_continuation():
    """申报条件被 PDF 插在适应症中间时，不能把下一页的续行一起删掉。"""
    from indication_regex import _split_numbered

    block = (
        "1.适应症一。2.适应症二。"
        "3.本品联合紫杉醇和铂类药物或含"
        "1.2025年12月31日协议到期，且不申请调整医保支付范围的谈判药品。"
        "2.2025年12月31日协议到期，适应症或功能主治未发生重大变化，因适应症与医保支付范围不一致，主动申请调整支付范围的谈判药品。"
        "3.2020年1月1日至2025年6月30日期间，经国家药监部门批准，适应症或功能主治发生重大变化，主动申请调整医保支付范围的谈判药品和目录内其他药品。"
        "氟尿嘧啶类和铂类药物用于不可切除的局部晚期、复发或转移性食管鳞状细胞癌的一线治疗。"
        "4.本品适用于既往接受过一线标准化疗后进展或不可耐受的局部晚期或转移性食管鳞状细胞癌的治疗。"
    )
    parts = _split_numbered(block)
    assert len(parts) == 4
    assert parts[2].startswith("3.本品联合紫杉醇和铂类药物或含氟尿嘧啶类")
    assert parts[3].startswith("4.")


def test_approval_date_requires_evidence_sentence():
    from approval_dates import resolve_from_hits

    hits = [
        {"title": "鳞状 NSCLC 一线获批", "content": "2021年1月13日，NMPA 批准替雷利珠单抗联合化疗用于一线治疗晚期鳞状非小细胞肺癌。", "url": "https://a.com/x"},
        {"title": "鳞状 NSCLC 获批", "content": "2021年1月13日，替雷利珠单抗一线鳞状非小细胞肺癌获批。", "url": "https://b.com/y"},
        {"title": "上市", "content": "替雷利珠单抗于2019年12月获批上市，用于经典型霍奇金淋巴瘤。", "url": "https://c.com/z"},
    ]
    got = resolve_from_hits(
        hits, "非小细胞肺癌",
        "本品联合紫杉醇和卡铂用于局部晚期或转移性鳞状非小细胞肺癌患者的一线治疗", "2019-12",
    )
    assert got and got["date"] == "2021-01-13"


def test_approval_date_rejects_single_source_and_mismatched_qualifier():
    from approval_dates import resolve_from_hits

    one = [{"title": "获批", "content": "2021年6月，本品一线治疗非小细胞肺癌获批。", "url": "https://a.com"}]
    assert resolve_from_hits(one, "非小细胞肺癌", "一线治疗非小细胞肺癌", "") is None

    # 限定词不一致：原文是 HER2 尿路上皮，证据句没有 HER2
    mismatch = [
        {"title": "尿路上皮癌", "content": "2022年3月，本品含铂化疗失败后尿路上皮癌获批。", "url": "https://a.com"},
        {"title": "尿路上皮癌", "content": "2022年3月，含铂化疗失败的尿路上皮癌获批。", "url": "https://b.com"},
    ]
    assert resolve_from_hits(mismatch, "尿路上皮癌", "联合维迪西妥单抗治疗 HER2 表达的尿路上皮癌", "") is None

    # 与首次上市同月的“首次上市”句不能当适应症日期
    launch = [
        {"title": "上市", "content": "2024年5月，本品首次上市，用于非鳞状非小细胞肺癌的一线治疗。", "url": "https://a.com"},
        {"title": "上市", "content": "2024年5月，本品首次获批上市，用于非鳞状非小细胞肺癌。", "url": "https://b.com"},
    ]
    assert resolve_from_hits(launch, "非小细胞肺癌", "一线治疗非鳞状非小细胞肺癌", "2024-05") is None


def test_split_numbered_indications_drops_shared_approval_date():
    rows = _split_numbered_indications([{
        "indication_short": "非小细胞肺癌",
        "indication_full": "【1】非鳞状非小细胞肺癌：本品联合培美曲塞。【2】鳞状非小细胞肺癌：本品联合紫杉醇。",
        "approval_date": "2024-10-16",
        "approval_country": "中国",
    }])
    assert len(rows) == 2
    assert all(r["approval_date"] == "" for r in rows)


def test_short_cleanup_repairs_fragment_without_dictionary():
    from agent import _regex_short_cleanup

    products = [{"indications": [
        {
            "indication_full": "2.作为鼻内皮质类固醇的附加维持治疗药物，用于治疗全身性皮质类固醇和/或手术治疗无法充分控制疾病的慢性鼻窦炎伴鼻息肉（CRSwNP）成人患者；",
            "indication_short": "或手术治疗无法充分控制疾病",
        },
        {
            "indication_full": "与伊匹木单抗联合用于：不可切除或晚期肝细胞癌（HCC）成人患者的一线治疗",
            "indication_short": "与伊匹木单抗",
        },
        {
            "indication_full": "本品适用于不可切除或转移性黑色素瘤的一线治疗；",
            "indication_short": "黑色素瘤",
        },
    ]}]
    _regex_short_cleanup(products)
    assert [i["indication_short"] for i in products[0]["indications"]] == [
        "慢性鼻窦炎伴鼻息肉", "肝细胞癌", "黑色素瘤",
    ]


def test_split_circled_sub_indications():
    from indication_regex import _split_numbered, _short

    block = ("【3】非小细胞肺癌：①非鳞状非小细胞肺癌：本品联合培美曲塞和卡铂用于一线治疗。"
             "②鳞状非小细胞肺癌：本品联合卡铂和白蛋白紫杉醇用于一线治疗。")
    parts = _split_numbered(block)
    assert len(parts) == 2
    assert [_short(p) for p in parts] == ["非小细胞肺癌", "非小细胞肺癌"]
    assert "①" in parts[0] and "②" in parts[1]
    assert "本品联合培美曲塞" in parts[0] and "本品联合卡铂" in parts[1]


def test_disease_covered_by_full_text():
    from agent import _disease_covered

    rows = [{
        "indication_full": "与伊匹木单抗联合用于不可切除或晚期肝细胞癌（HCC）成人患者的一线治疗、"
                           "不可切除或转移性结直肠癌患者的一线治疗、不可手术切除的恶性胸膜间皮瘤成人患者",
    }]
    assert _disease_covered("肝癌", rows)
    assert _disease_covered("结直肠癌", rows)
    assert _disease_covered("恶性胸膜间皮瘤", rows)
    assert not _disease_covered("胃癌", rows)


def test_flatten_overseas_insurance_not_applicable(monkeypatch):
    monkeypatch.setattr(main, "_match_nhi", lambda *a, **k: {
        "price_medical_insurance": "*", "catalog_type": "基本医保",
        "insurance_category": "乙", "restriction_timeline": {},
    })
    data = {
        "generic_name": "测试药",
        "products": [{
            "generic_name": "测试药", "brand_name": "品牌", "manufacturer": "厂家",
            "indications": [{
                "indication_short": "胃癌", "indication_full": "胃癌",
                "approval_country": "美国", "approval_date": "2020-01",
            }],
        }],
    }
    rows = main.flatten_to_rows(data)
    assert rows[0]["insurance_status_text"] == "不适用"


def test_normalize_compat_ideographs():
    from indication_regex import _normalize_compat

    assert _normalize_compat("说明书⽤法⽤量") == "说明书用法用量"
    assert _normalize_compat("纳⼊《国家基本药物⽬录》") == "纳入《国家基本药物目录》"


def test_split_unnumbered_keeps_subpopulation_sentences():
    from indication_regex import _split_numbered

    envafolimab = ("适用于不可切除或转移性微卫星高度不稳定（MSI-H）或错配修复基因缺陷型（dMMR）的成人晚期实体瘤患者。"
                   "包括既往经过氟尿嘧啶类、奥沙利铂和伊立替康治疗后出现疾病进展的晚期结直肠癌患者；"
                   "既往治疗后出现疾病进展且无满意替代治疗方案的其他晚期实体瘤患者。")
    assert len(_split_numbered(envafolimab)) == 1

    inebilizumab = ("本品适用于抗水通道蛋白4（AQP4）抗体阳性的视神经脊髓炎谱系疾病（NMOSD）成人患者的治疗。"
                    "本品适用于免疫球蛋白G4相关性疾病（IgG4-RD）成人患者。"
                    "本品与常规治疗药物联合用于治疗成人全身型重症肌无力（gMG）患者。")
    assert len(_split_numbered(inebilizumab)) == 3


def test_ppt_brand_strips_generic_label(monkeypatch):
    import io, json
    from types import SimpleNamespace
    from pptx import Presentation
    from pptx.util import Inches
    import skill_vision_parser as svp

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text = "商品名：恩维达®\n通用名：恩沃利单抗注射液"
    buf = io.BytesIO(); prs.save(buf)

    fake = SimpleNamespace(text=json.dumps({"indication_approval_times": [], "guidelines": []}, ensure_ascii=False))
    monkeypatch.setattr(svp.llm_client, "chat", lambda *a, **k: fake)
    res = svp._extract_ppt_info(buf.getvalue())
    assert res["brand_name"] == "恩维达"


def test_numbering_style_priority():
    from indication_regex import _split_numbered, _short

    right_paren = ("1）本品单药用于既往接受过二线及以上化疗失败的复发或转移性鼻咽癌患者的治疗。"
                   "2）本品联合顺铂和吉西他滨用于复发或转移性鼻咽癌患者的一线治疗。")
    parts = _split_numbered(right_paren)
    assert len(parts) == 2
    assert [_short(p) for p in parts] == ["鼻咽癌", "鼻咽癌"]

    labels = ("头颈部鳞状细胞癌：本品与含铂化疗联合用于复发性头颈部鳞状细胞癌的一线治疗。"
              "肝细胞癌：本品联合贝伐珠单抗用于不可切除或转移性肝细胞癌的治疗。")
    parts = _split_numbered(labels)
    assert [_short(p) for p in parts] == ["头颈部鳞状细胞癌", "肝细胞癌"]

    # 有【1】就不用里面的 1)
    nested = "【1】胃癌：本品联合化疗。1）一线治疗。2）二线治疗。【2】食管癌：本品单药。"
    parts = _split_numbered(nested)
    assert len(parts) == 2
    assert parts[0].startswith("【1】") and parts[1].startswith("【2】")


def test_drop_web_rows_covered_by_official():
    rows = [
        {"generic_name": "赛帕利单抗注射液", "indication_short": "经典型霍奇金淋巴瘤",
         "indication_full": "经典型霍奇金淋巴瘤本品适用于至少经过二线系统化疗的复发或难治性经典型霍奇金淋巴瘤",
         "nhsa_pdf_url": "http://x/a.pdf"},
        {"generic_name": "赛帕利单抗", "indication_short": "经典型霍奇金淋巴瘤",
         "indication_full": "复发或难治性经典型霍奇金淋巴瘤", "nhsa_pdf_url": ""},
        {"generic_name": "赛帕利单抗", "indication_short": "宫颈癌",
         "indication_full": "PD-L1表达阳性的宫颈癌", "nhsa_pdf_url": ""},
    ]
    result = main._drop_web_covered_rows(rows)
    assert len(result) == 2
    assert [r["indication_short"] for r in result] == ["经典型霍奇金淋巴瘤", "宫颈癌"]
    assert result[1]["generic_name"] == "赛帕利单抗"


def test_heading_style_and_bracket_labels():
    from indication_regex import _split_numbered, _short

    headings = "【特应性皮炎】本品用于治疗特应性皮炎。【结节性痒疹】本品用于治疗结节性痒疹。【哮喘】本品用于哮喘维持治疗。"
    assert [_short(p) for p in _split_numbered(headings)] == ["特应性皮炎", "结节性痒疹", "哮喘"]

    square = "[头颈部鳞状细胞癌]本品联合化疗用于一线治疗。[肝细胞癌]本品联合贝伐珠单抗用于肝细胞癌治疗。"
    assert [_short(p) for p in _split_numbered(square)] == ["头颈部鳞状细胞癌", "肝细胞癌"]

    # 没有冒号：病名直接接“本品”
    no_colon = ("经典型霍奇金淋巴瘤本品适用于至少经过二线系统化疗的复发或难治性经典型霍奇金淋巴瘤。"
                "宫颈癌本品适用于既往接受含铂化疗治疗失败的复发或转移性宫颈癌。")
    assert [_short(p) for p in _split_numbered(no_colon)] == ["霍奇金淋巴瘤", "宫颈癌"]


def test_numbered_sub_items_are_not_indications():
    from indication_regex import _split_numbered, _short

    asthma = ("【哮喘】本品适用于6岁及以上儿童和成人哮喘患者的维持治疗，其中包括："
              "1）经中-高剂量吸入性糖皮质激素（ICS）联合其他哮喘控制药物治疗后仍控制不佳的哮喘患者；"
              "2）口服糖皮质激素依赖性的哮喘患者。")
    parts = _split_numbered(asthma)
    assert len(parts) == 1
    assert _short(parts[0]) == "哮喘"


def test_web_inds_only_recent_and_uncovered():
    from agent import _drop_covered_web_inds, _keep_recent_web_inds, _recent_cutoff

    existing = [{"indication_short": "霍奇金淋巴瘤", "indication_full": "经典型霍奇金淋巴瘤，至少二线化疗后"}]
    inds = [
        {"indication_short": "霍奇金淋巴瘤", "indication_full": "复发或难治性霍奇金淋巴瘤", "approval_date": "2026-01"},
        {"indication_short": "宫颈癌", "indication_full": "PD-L1阳性宫颈癌", "approval_date": "2026-01"},
        {"indication_short": "鼻咽癌", "indication_full": "鼻咽癌一线", "approval_date": "2026-01"},
    ]
    kept = _drop_covered_web_inds(inds, existing)
    assert [i["indication_short"] for i in kept] == ["宫颈癌", "鼻咽癌"]

    cutoff = "2025-11"
    recent = _keep_recent_web_inds(
        [{"approval_date": "2026-01"}, {"approval_date": "2024-06"}, {"approval_date": ""}], cutoff
    )
    assert len(recent) == 1
    assert len(_recent_cutoff()) == 7


def test_add_latest_generics(monkeypatch):
    import agent

    monkeypatch.setattr(agent, "ds_search", lambda *a, **k: [{"title": "x", "content": "新获批 安健宁"}])
    monkeypatch.setattr(agent, "_call_llm", lambda prompt, cls, **kwargs: cls(generics=[{
        "brand_name": "安健宁", "manufacturer": "海正博锐", "approval_date": "2026-01",
    }]))
    monkeypatch.setattr(agent, "_brand_matches_generic", lambda *a, **k: True)

    products = [{
        "generic_name": "阿达木单抗注射液", "brand_name": "修美乐", "manufacturer": "艾伯维",
        "origin_type": "原研", "origin_region": "进口", "dosage_form": "注射剂",
        "indications": [{"indication_short": "类风湿关节炎", "indication_full": "类风湿关节炎",
                         "approval_country": "中国", "approval_date": ""}],
    }]
    agent._add_latest_generics(products, "阿达木单抗注射液", "2025-11", {"修美乐"})
    assert len(products) == 2
    generic = products[1]
    assert generic["origin_type"] == "仿制"
    assert generic["brand_name"] == "安健宁"
    assert generic["indications"][0]["indication_short"] == "类风湿关节炎"


def test_generic_rows_survive_web_dedupe():
    official = {
        "generic_name": "阿达木单抗注射液", "indication_short": "类风湿关节炎",
        "indication_full": "类风湿关节炎", "origin_type": "原研",
        "nhsa_pdf_url": "http://x/a.pdf",
    }
    generic = {
        "generic_name": "阿达木单抗注射液", "indication_short": "类风湿关节炎",
        "indication_full": "类风湿关节炎", "origin_type": "仿制", "manufacturer": "海正博锐",
        "nhsa_pdf_url": "",
    }
    rows = [official, generic]
    assert len(main._drop_web_covered_rows(rows)) == 2
    assert len(main._drop_web_full_text_duplicates(rows)) == 2


def test_approved_time_not_changed_by_later_edit(client):
    import time
    from db import SessionLocal
    from models import Drug

    payload = {
        "generic_name": "PYTEST_TIME_DRUG", "brand_name": "品牌", "manufacturer": "厂家",
        "indication_short": "测试适应症", "indication_full": "测试适应症",
    }
    try:
        draft = client.post("/drugs/draft", json=payload).json()
        approved = client.post(f"/drugs/{draft['id']}/approve").json()
        approved_time = approved["updated_at"]
        time.sleep(1.1)
        edited = client.patch(f"/drugs/{draft['id']}", json={"packaging_spec": "10支/盒"}).json()
        assert edited["updated_at"] == approved_time
    finally:
        with SessionLocal() as s:
            s.query(Drug).filter(Drug.generic_name == "PYTEST_TIME_DRUG").delete()
            s.commit()


def test_split_drug_names_batch():
    raw = ("替雷利珠单抗 信迪利单抗 卡瑞利珠单抗 特瑞普利单抗 帕博利珠单抗 阿替利珠单抗 "
           "度伐利尤单抗 纳武利尤单抗 卡度尼利单抗 斯鲁利单抗 依沃西单抗 贝莫苏拜单抗 "
           "阿得贝利单抗 恩沃利单抗 普特利单抗 索卡佐利单抗 舒格利单抗 瑞拉芙普α注射液 "
           "派安普利单抗 赛帕利单抗 恩朗苏拜单抗 菲诺利单抗")
    names = main.split_drug_names(raw)
    assert len(names) == 22
    assert names[0] == "替雷利珠单抗"
    assert names[-1] == "菲诺利单抗"
    assert "瑞拉芙普α注射液" in names
    assert main.split_drug_names("A，B、C;D\nE") == ["A", "B", "C", "D", "E"]


def test_generic_headings_stay_one_row_each():
    from indication_regex import _split_numbered, _short

    block = ("非小细胞肺癌：1.【联合治疗】：①本品联合培美曲塞和卡铂用于转移性非鳞状非小细胞肺癌；"
             "②本品联合紫杉醇和卡铂用于转移性鳞状非小细胞肺癌。"
             "2.【单药治疗】：本品用于Ⅲ期非小细胞肺癌的治疗。结外NK/T细胞淋巴瘤：本品单药治疗。")
    parts = _split_numbered(block)
    assert len(parts) == 3
    assert [_short(p) for p in parts] == ["非小细胞肺癌", "非小细胞肺癌", "结外NK/T细胞淋巴瘤"]
    assert "①" in parts[0] and "②" in parts[0]
    assert "【联合治疗】" in parts[0] and "【单药治疗】" in parts[1]
