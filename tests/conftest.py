"""测试用固定开关：不让本地 .env 的 LLM 试点配置影响离线用例。"""
import os

os.environ["INDICATION_MODE"] = "regex"
os.environ["INDICATION_DICTS"] = "on"
os.environ["INSURANCE_ALIASES"] = "on"
os.environ["LLM_OVERWRITE_APPROVED"] = "0"
os.environ["LLM_INDICATION_THINKING"] = "0"
