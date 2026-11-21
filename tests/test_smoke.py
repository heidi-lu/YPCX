"""Offline smoke tests. No LLM, network, or live GitHub calls."""

import pytest
from fastapi.testclient import TestClient

import main
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


def test_drug_to_dict_strips_control_characters():
    drug = Drug(generic_name="测试药", brand_name="品\x00牌")
    result = main._drug_to_dict(drug)
    assert result["brand_name"] == "品牌"
