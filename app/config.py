"""
全局配置：从环境变量读取，提供模型名映射。
"""
import os
from dotenv import load_dotenv

load_dotenv(override=True)

DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
PROXY_API_KEY: str = os.getenv("PROXY_API_KEY", "").strip()
PORT: int = int(os.getenv("PORT", "8000"))

# 模型名映射表：将旧/别名模型名规范化为 DeepSeek 官方支持的模型名
_MODEL_ALIAS: dict[str, str] = {
    "deepseek-v4-flash":  "deepseek-v4-flash",
    "deepseek-v4-pro":    "deepseek-v4-pro",
    "deepseek-chat":      "deepseek-v4-flash",
    "deepseek-reasoner":  "deepseek-v4-flash",
    "deepseek-v3":        "deepseek-v4-flash",
    "deepseek-v4":        "deepseek-v4-flash",
    "deepseek-v4-5":      "deepseek-v4-flash",
}


def resolve_model(name: str) -> str:
    """将任意模型名解析为 DeepSeek API 实际支持的模型名。"""
    lower = name.lower()
    if lower in _MODEL_ALIAS:
        return _MODEL_ALIAS[lower]
    if "pro" in lower:
        return "deepseek-v4-pro"
    if lower.startswith("deepseek"):
        return "deepseek-v4-flash"
    return DEEPSEEK_MODEL
