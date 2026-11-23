from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Boolean, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

class Drug(Base):
    __tablename__ = "drugs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    generic_name = Column(String(200), index=True, nullable=False, default="")
    brand_name = Column(String(200), index=True, nullable=False, default="")
    manufacturer = Column(String(300), nullable=False, default="")
    drug_type = Column(String(200), default="")
    origin_type = Column(String(50), default="") 

    registration_category = Column(String(100), default="")

    indication_short = Column(String(300), nullable=False, default="")
    indication_subtype = Column(String(300), default="")
    indication_full = Column(Text, default="")
    approval_country = Column(String(50), default="中国")
    
    china_approval_date = Column(String(100), default="")          # 药品上市时间
    indication_approval_date = Column(String(100), default="")     # 适应症获批时间
    indication_approval_source = Column(String(500), default="")   # 获批时间来源 URL（外部数据必须留源）
    drug_insurance_date = Column(String(100), default="")          # 药品（通用名）最早进医保时间
    indication_insurance_date = Column(String(100), default="")    # 适应症进医保时间/兼容字段
    insurance_status_text = Column(String(200), default="")        # 例如：2021年进入，2026年退出

    guideline_name = Column(String(300), default="")
    guideline_recommendation = Column(Text, default="")

    dosage = Column(Text, default="")
    dosage_single = Column(String(100), default="")
    frequency = Column(String(100), default="")
    duration = Column(Text, default="")
    route = Column(String(100), default="")

    dosage_form = Column(String(100), default="")
    specification = Column(String(100), default="")
    packaging_spec = Column(String(500), default="")

    insurance_category = Column(String(100), default="")
    insurance_restriction = Column(Text, default="")

    price_market = Column(String(300), default="")
    price_insurance = Column(String(200), default="")   # 医保支付标准
    origin_region = Column(String(10), default="")      # 国产 / 进口
    # 👇 彻底移除 market_data (JSON) 字段
    nhsa_pdf_url = Column(String(500), default="")
    nhsa_ppt_url = Column(String(500), default="")
    fetch_time = Column(String(50), default="")

    status = Column(String(20), default="MACHINE")
    manual_locked_fields = Column(JSON, default=list)
    is_deleted = Column(Boolean, default=False, index=True)

    created_at = Column(DateTime, default=datetime.now)
    # 入库时间：只在“入库”那一刻写；后续补全/重查补字段不刷新
    updated_at = Column(DateTime, default=datetime.now)

class LlmUsage(Base):
    """每次 LLM 调用一条（含失败尝试），用于成本核算与回归复盘。"""
    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    drug = Column(String(200), default="", index=True)
    stage = Column(String(50), default="", index=True)
    provider = Column(String(50), default="")
    model = Column(String(100), default="", index=True)
    thinking = Column(Boolean, default=False)
    attempt = Column(Integer, default=1)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    reasoning_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    cost_cny = Column(Float, nullable=True)
    status = Column(String(20), default="ok")
    error = Column(Text, default="")
    snapshot_key = Column(String(64), default="")


class SearchUsage(Base):
    """每次网页搜索一条。DeepSeek 只对成功且有结果的请求计费。"""
    __tablename__ = "search_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    drug = Column(String(200), default="", index=True)
    query = Column(Text, default="")
    provider = Column(String(50), default="")
    result_count = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    cost_cny = Column(Float, nullable=True)
    status = Column(String(20), default="ok")
    snapshot_key = Column(String(64), default="")


class Feedback(Base):
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(50))
    drug = Column(String(200))
    indication = Column(String(300))
    field = Column(String(100))
    current = Column(Text)
    correct = Column(Text)
    missing_type = Column(String(100))
    description = Column(Text)
    source = Column(String(500))
    time = Column(String(50))
    created_at = Column(DateTime, default=datetime.now)