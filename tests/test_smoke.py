"""Offline smoke tests. No LLM, network, or live GitHub calls."""

import pytest
from fastapi.testclient import TestClient

import main
from agent import (
    _clean_brand_name,
    _is_chinese_brand,
    _split_numbered_indications,
    _strength_from_text,
)
from skill_vision_parser import _extract_full_indications_from_text
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
    assert rows[0]["insurance_date"] == "2017年"
    # 适应症 2020 年才获批，不能早于获批时间
    assert rows[0]["indication_insurance_date"] == "2020年1月1日"


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
    assert {r["indication_short"] for r in rows} == {"过敏性哮喘", "重度嗜酸粒细胞性哮喘"}

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


def test_full_indications_from_pdf_text():
    text = """说明书全部适应症/功能主治
【1】经典型霍奇金淋巴瘤 本品适用于至少经过二线系统化疗的复发或难治性经典型霍奇金淋巴瘤的治疗。
【2】非鳞状非小细胞肺癌 （1）本品联合培美曲塞和铂类化疗，用于不可手术切除的局部晚期患者。
现行医保目录的医保支付范围
1.经典型霍奇金淋巴瘤..."""
    rows = _extract_full_indications_from_text(text)
    assert [r["indication_short"] for r in rows] == ["经典型霍奇金淋巴瘤", "非鳞状非小细胞肺癌"]
    assert rows[0]["indication_full"].startswith("【1】")
    assert rows[1]["indication_full"].startswith("【2】")


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
