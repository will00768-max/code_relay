"""
全局日志配置，在 app 包导入时自动初始化。
"""
import logging
import os

_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "relay.log")
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# delay=True：首次写入时才创建文件，不持续占用句柄
_file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8", delay=True)
_file_handler.setFormatter(_fmt)
_file_handler.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)
_console_handler.setLevel(logging.INFO)

logging.basicConfig(level=logging.DEBUG, handlers=[_console_handler])

# 根 logger 挂载文件 handler
logging.getLogger().addHandler(_file_handler)

# 屏蔽 watchfiles 自身的 DEBUG 日志（它会把每次文件变化写进来，造成死循环）
logging.getLogger("watchfiles").setLevel(logging.WARNING)
