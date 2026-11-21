from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
import json, re, os, sys, requests
from bs4 import BeautifulSoup
from openai import OpenAI
from datetime import datetime

# 把根目录加进 sys.path，方便引到外面的 config.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 👇 强行导入 engine 和 MarketBase
from market_module.models import MarketInsight, SessionLocalMarket, MarketBase, engine
from config import MAIN_LLM_API_KEY, MAIN_LLM_BASE_URL, MAIN_LLM_MODEL

app = FastAPI(title="市场信息独立服务")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 👇 核心修复：服务启动时强行建表
@app.on_event("startup")
def startup():
    print("========================================")
    print("🚀 [Market DB] 正在检查并初始化市场信息表...")
    try:
        MarketBase.metadata.create_all(bind=engine)
        print("✅ [Market DB] 数据库表初始化完成！")
    except Exception as e:
        print(f"❌ [Market DB] 初始化失败: {e}")
    print("========================================")

def get_market_db():
    db = SessionLocalMarket()
    try:
        yield db
    finally:
        db.close()

_client = OpenAI(api_key=MAIN_LLM_API_KEY, base_url=MAIN_LLM_BASE_URL)

class MarketDataIn(BaseModel):
    generic_name: str
    manufacturer: str = ""
    indication: str = ""
    metric_type: str = "销售额"
    metric_value: Optional[float] = None
    metric_unit: str = "亿元"
    metric_year: str
    metric_period: str = "全年"
    data_source: str = ""
    source_snippet: str = ""
    source_url: str = ""

class ParseReq(BaseModel):
    text: str

# 辅助函数：抓取网页正文
def fetch_web_content(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator='\n', strip=True)
        return text[:10000]
    except Exception as e:
        print(f"抓取网页失败: {e}")
        return ""

# ================= 接口 1：查询市场大盘 =================
@app.get("/api/market_facts")
def get_market_facts(keyword: str = "", year: str = "", db: Session = Depends(get_market_db)):
    try:
        query = db.query(MarketInsight)
        
        if keyword and keyword.strip():
            search = f"%{keyword.strip()}%"
            query = query.filter(
                (MarketInsight.generic_name.like(search)) | 
                (MarketInsight.manufacturer.like(search)) | 
                (MarketInsight.indication.like(search))
            )
            
        if year and year != "all" and year.strip():
            query = query.filter(MarketInsight.metric_year == year.strip())
            
        facts = query.order_by(MarketInsight.metric_year.desc(), MarketInsight.metric_value.desc()).all()
        
        result = []
        for f in facts:
            item = {}
            for c in f.__table__.columns:
                val = getattr(f, c.name)
                if val is None:
                    item[c.name] = ""
                elif isinstance(val, datetime):
                    item[c.name] = val.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    item[c.name] = val
            result.append(item)
            
        return result

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

# ================= 接口 2：单条入库 =================
@app.post("/api/market_facts")
def insert_market_fact(data: MarketDataIn, db: Session = Depends(get_market_db)):
    try:
        existing = db.query(MarketInsight).filter(
            MarketInsight.generic_name == data.generic_name,
            MarketInsight.metric_year == data.metric_year,
            MarketInsight.metric_period == data.metric_period,
            MarketInsight.metric_type == data.metric_type
        ).first()
        
        if existing:
            for k, v in data.model_dump().items():
                setattr(existing, k, v)
        else:
            db.add(MarketInsight(**data.model_dump()))
        
        db.commit()
        return {"status": "success", "message": "入库成功"}
    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"入库失败: {str(e)}")

# ================= 接口 3：AI 智能解析文本/链接 =================
@app.post("/api/parse_article")
def parse_article(req: ParseReq):
    content = req.text
    source_url = ""
    
    if content.startswith("http"):
        source_url = content
        content = fetch_web_content(source_url)
        if not content:
            raise HTTPException(status_code=400, detail="无法获取网页正文，请确认链接是否有效或需登录")

    content = content[:10000]

    prompt = f"""你是一个专业的医药市场分析师。请仔细阅读以下长文本，从中提取**所有**提到的药品市场销售/份额数据。
    严格输出 JSON 格式的数组，不要只提取一条，要把文章里提到的所有药品数据都列出来：
    {{
        "data": [
            {{
                "generic_name": "药品通用名（请保留剂型，如：司美格鲁肽注射液、依达拉奉右莰醇注射用浓溶液）",
                "manufacturer": "生产厂家",
                "indication": "核心适应症",
                "metric_type": "指标类型(主要是销售额)",
                "metric_value": 数值(纯数字，不要带单位),
                "metric_unit": "单位(如亿元/%)",
                "metric_year": "数据年份(如2025)",
                "metric_period": "周期(如全年/Q1/累计)",
                "data_source": "数据来源(如米内网/公司公告)",
                "source_snippet": "提取该数据的原文原句(作为防幻觉的证据)"
            }}
        ]
    }}
    【死命令】：
    1. 只要文章里出现了“销售额达XX亿元”、“收入总计XX亿元”，必须提取。
    2. 如果没有具体的数字金额，不要瞎编，直接跳过该项。
    
    文章内容：
    {content}"""

    try:
        resp = _client.chat.completions.create(
            model=MAIN_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        result_text = resp.choices[0].message.content
        data = json.loads(result_text)
        
        extracted_list = data.get("data", [])
        
        if source_url:
            for item in extracted_list:
                item["source_url"] = source_url
                
        return {"status": "success", "data": extracted_list}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))