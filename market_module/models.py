# models.py
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MARKET_DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'market.db')}"

engine = create_engine(MARKET_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocalMarket = sessionmaker(bind=engine, autoflush=False, autocommit=False)

MarketBase = declarative_base()

class MarketInsight(MarketBase):
    __tablename__ = "market_insight"

    id = Column(Integer, primary_key=True, autoincrement=True)
    generic_name = Column(String(200), index=True, nullable=False)
    manufacturer = Column(String(200), default="")
    indication = Column(String(300), default="")
    
    metric_type = Column(String(100), default="销售额")
    metric_value = Column(Float, nullable=True)
    metric_unit = Column(String(50), default="亿元")
    
    metric_year = Column(String(20), index=True)
    metric_period = Column(String(50), default="全年")
    
    data_source = Column(String(200), default="")
    source_snippet = Column(Text, default="")
    source_url = Column(String(1000), default="")
    
    created_at = Column(DateTime, default=datetime.now)