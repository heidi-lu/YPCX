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
    # 👇 彻底移除 market_data (JSON) 字段
    nhsa_pdf_url = Column(String(500), default="")
    nhsa_ppt_url = Column(String(500), default="")
    fetch_time = Column(String(50), default="")

    status = Column(String(20), default="MACHINE")
    manual_locked_fields = Column(JSON, default=list)
    is_deleted = Column(Boolean, default=False, index=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

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