import json
import logging
from pydantic import BaseModel, Field
from openai import OpenAI

from deepseek_search import search as ds_search
from config import MARKET_LLM_API_KEY, MARKET_LLM_BASE_URL, MARKET_LLM_MODEL

logger = logging.getLogger(__name__)
_client = OpenAI(api_key=MARKET_LLM_API_KEY, base_url=MARKET_LLM_BASE_URL)

class MarketSummaryResult(BaseModel):
    sales: str = Field(default="")
    position: str = Field(default="")
    trend: str = Field(default="")
    channel: str = Field(default="")

def fetch_market_info(drug_name: str, generic_name: str = "", origin_type: str = "") -> dict:
    if not drug_name:
        return {}

    if origin_type == "仿制":
        logger.info(f"📊 [市场信息] 仿制药跳过抓取: {drug_name}")
        return {}

    # 👇 优化：如果有商品名，直接用商品名搜，极简最高效
    if drug_name and drug_name != generic_name:
        search_term = f"{drug_name} 销售额 亿元"
    else:
        search_term = f"{generic_name} 销售额 亿元"
    
    logger.info(f"📊 [市场信息] 搜索: {search_term}")
    hits = ds_search(search_term, max_results=3) 
    
    if not hits:
        logger.warning(f"⚠️ [市场信息] 未搜到任何相关数据: {drug_name}")
        return {}

    sources = [{"title": h.get("title", ""), "url": h.get("url", "")} for h in hits]

    prompt = f"""你是医药市场分析师。请从以下资料提取【{drug_name}】的市场数据。严格输出JSON：
{{
    "sales": "提取国内终端销售额（必须带'亿元/万元'，否则填''）",
    "position": "提取市场排名或份额（必须带'第X/前X/%'，否则填''）",
    "trend": "提取增速或趋势（必须带'%'或'增长/下降'，否则填''）",
    "channel": "提取销售渠道（如'院内/院外/DTP'，否则填''）"
}}
【绝对死律】：
1. 严禁拿【全球销售额】充数！必须是【中国/国内】数据。
2. 资料中没有出现具体数字的，该字段绝对不准写文字，必须填 ''！
资料：
{hits}"""

    try:
        resp = _client.chat.completions.create(
            model=MARKET_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        res_json = json.loads(resp.choices[0].message.content or "{}")
        
        valid_summaries = {k: v for k, v in res_json.items() if v and str(v).strip() not in ["暂无", "无", "未提及", ""]}
        
        if not valid_summaries:
            logger.warning(f"⚠️ [市场信息] 未搜到有效数字: {drug_name}")
            return {"summaries": {"sales": "", "position": "", "trend": "", "channel": ""}, "sources": sources}

        logger.info(f"🎉 [市场信息] 提取成功: {valid_summaries}")
        return {"summaries": valid_summaries, "sources": sources}
    except Exception as e:
        logger.error(f"❌ [市场信息] 提取失败: {e}")
        return {}