import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import app  # noqa: F401 — 触发日志配置与数据库初始化
from app.config import PORT
from app.routes.admin import router as admin_router
from app.routes.proxy import router as proxy_router

app = FastAPI(title="Codex → DeepSeek 代理", version="2.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin_router)
app.include_router(proxy_router)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=True,
        # 只监视 app/ 目录，根目录的 relay.log / *.db 不在监视范围内
        reload_dirs=["app"],
    )
