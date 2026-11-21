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
SEARCH_MAX_CONCURRENCY = int(os.getenv("SEARCH_MAX_CONCURRENCY", "5"))
HTTP_RETRY = int(os.getenv("HTTP_RETRY", "2"))
GARBAGE_RATIO_THRESHOLD = float(os.getenv("GARBAGE_RATIO_THRESHOLD", "0.2"))

try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                               decode_responses=True, socket_connect_timeout=2)
    redis_client.ping()
except Exception:
    redis_client = None