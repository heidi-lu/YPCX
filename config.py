import os
import logging
import redis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 1. 主文本大模型（基础信息/适应症抽取）
MAIN_LLM_API_KEY = os.getenv("MAIN_LLM_API_KEY", "")
MAIN_LLM_BASE_URL = os.getenv("MAIN_LLM_BASE_URL", "https://api.deepseek.com")
MAIN_LLM_MODEL = os.getenv("MAIN_LLM_MODEL", "deepseek-chat")

# 2. 网页搜索
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY") or MAIN_LLM_API_KEY
SEARCH_BASE_URL = os.getenv("SEARCH_BASE_URL", "https://api.deepseek.com/v1/tools/search")

# 3. 市场信息 LLM
MARKET_LLM_API_KEY = os.getenv("MARKET_LLM_API_KEY") or MAIN_LLM_API_KEY
MARKET_LLM_BASE_URL = os.getenv("MARKET_LLM_BASE_URL", "https://api.deepseek.com")
MARKET_LLM_MODEL = os.getenv("MARKET_LLM_MODEL", "deepseek-chat")

# 4. 视觉大模型（PDF 解析）
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl-plus")

# 5. 数据库与 Redis
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'drug_agent.db')}")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "3"))
QUERY_CONCURRENCY = int(os.getenv("QUERY_CONCURRENCY", "3"))
QUERY_MAX_BATCH = int(os.getenv("QUERY_MAX_BATCH", "50"))
SEARCH_MAX_CONCURRENCY = int(os.getenv("SEARCH_MAX_CONCURRENCY", "5"))
HTTP_RETRY = int(os.getenv("HTTP_RETRY", "2"))
GARBAGE_RATIO_THRESHOLD = float(os.getenv("GARBAGE_RATIO_THRESHOLD", "0.2"))

# 6. 调用路由 / 成本日志（P0）
# 阶段 -> 模型。默认全部沿用主模型；换成千问时只改这里或 .env，不改代码。
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", MAIN_LLM_MODEL)
SHORT_MODEL = os.getenv("SHORT_MODEL", EXTRACT_MODEL)
THINK_MODEL = os.getenv("THINK_MODEL", EXTRACT_MODEL)
MODEL_ROUTES = {
    "extract": EXTRACT_MODEL,
    "web": EXTRACT_MODEL,
    "brand": EXTRACT_MODEL,
    "dosage": SHORT_MODEL,
    "price": EXTRACT_MODEL,
    "think": THINK_MODEL,
    "vision": VISION_MODEL,
}
THINK_ENABLED = os.getenv("THINK_ENABLED", "0") == "1"
THINK_EFFORT = os.getenv("THINK_EFFORT", "high")

# 千问（DashScope）文本 key；默认复用视觉 key，同一账号。
QWEN_LLM_API_KEY = os.getenv("QWEN_LLM_API_KEY") or VISION_API_KEY
QWEN_LLM_BASE_URL = os.getenv("QWEN_LLM_BASE_URL", VISION_BASE_URL)

USAGE_LOG_ENABLED = os.getenv("USAGE_LOG_ENABLED", "1") != "0"
SNAPSHOT_ENABLED = os.getenv("SNAPSHOT_ENABLED", "1") != "0"
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", os.path.join(BASE_DIR, "snapshots"))

# 元 / 百万 token，2026-11 官方价（含税前的 USD 折算，缓存命中单独计价）。
# tier 按“单次请求输入 token”落档，命中哪档整单按哪档结算。
LLM_PRICES = {
    ("deepseek", "deepseek-flash"): {"in": 2.13, "out": 8.50, "cache": 0.043, "offpeak_half": True},
    ("deepseek", "deepseek-chat"): {"in": 2.13, "out": 8.50, "cache": 0.043, "offpeak_half": True},
    ("deepseek", "deepseek-v4-pro"): {"in": 9.40, "out": 28.10, "cache": 0.30, "offpeak_half": True},
    ("qwen", "qwen3.8-flash"): {"in": 0.80, "out": 2.70, "cache": 0.10},
    ("qwen", "qwen3.7-flash"): {"tiers": [
        {"max_in": 32_000, "in": 0.20, "out": 0.80, "cache": 0.04},
        {"max_in": 256_000, "in": 0.60, "out": 2.40, "cache": 0.12},
        {"max_in": 1_000_000, "in": 1.20, "out": 4.80, "cache": 0.24},
    ]},
    ("qwen", "qwen3.7-plus"): {"in": 1.96, "out": 7.82, "cache": 0.20},
    ("qwen", "qwen4-max"): {"in": 10.00, "out": 30.00, "cache": 0.10},
    ("qwen", "qwen3-vl-plus"): {"tiers": [
        {"max_in": 32_000, "in": 1.00, "out": 10.00, "cache": 0.20},
        {"max_in": 128_000, "in": 1.50, "out": 15.00, "cache": 0.30},
        {"max_in": 256_000, "in": 3.00, "out": 30.00, "cache": 0.60},
    ]},
}
# 元 / 次（成功且有结果才计费）
SEARCH_PRICES = {"deepseek": 0.0053, "qwen": 0.022}

try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                               decode_responses=True, socket_connect_timeout=2)
    redis_client.ping()
except Exception:
    redis_client = None
# 7. 适应症模式（P1 试点）
# regex：现有正则 + 字典；llm：大模型归一，跳过适应症映射字典
INDICATION_MODE = os.getenv("INDICATION_MODE", "regex").strip().lower()
# off 时隐藏：_CONCISE_RULES / _GUIDE_DISEASE_HINTS / _OVERSEAS_DISEASE_HINTS /
# _GENERIC_DISEASE_NAMES / ALIASES_DICT（医保 DISEASE_ALIASES 仍保留）
INDICATION_DICTS = os.getenv("INDICATION_DICTS", "on") != "off"
INSURANCE_ALIASES = os.getenv("INSURANCE_ALIASES", "on") != "off"
LLM_INDICATION_MODEL = os.getenv("LLM_INDICATION_MODEL", "qwen3.8-flash")
LLM_INDICATION_THINKING = os.getenv("LLM_INDICATION_THINKING", "0") == "1"

# 允许大模型结果覆盖 APPROVED 行的 short/subtype（默认关闭；回填时显式打开）
LLM_OVERWRITE_APPROVED = os.getenv("LLM_OVERWRITE_APPROVED", "0") == "1"
