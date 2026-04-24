"""
app 包初始化：日志配置 + 数据库建表。
"""
from app import logger  # noqa: F401 — 触发日志配置
from app.database import init_db

init_db()
