"""
管理路由：
  GET  /admin            → 管理面板 HTML（从 app/static/ 目录读取）
  GET  /admin/balance    → 查询 DeepSeek 账户余额
  GET  /admin/stats      → Token 统计
  GET  /admin/models     → 可用模型列表
  GET  /admin/model      → 当前默认模型
  POST /admin/model      → 设置默认模型
"""
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import app.config as cfg
from app.database import get_balance_stats, get_token_summary, save_balance_snapshot

logger = logging.getLogger(__name__)
router = APIRouter()

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@router.get("/", summary="健康检查")
async def health():
    return {"status": "ok", "service": "codex-deepseek-proxy"}


@router.get("/admin", summary="管理面板")
async def admin_panel():
    index_path = os.path.join(_STATIC_DIR, "index.html")
    return FileResponse(index_path, media_type="text/html")


@router.get("/admin/balance", summary="查询余额")
async def get_balance():
    if not cfg.DEEPSEEK_API_KEY:
        return JSONResponse({"error": "DEEPSEEK_API_KEY 未配置"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.get(
                f"{cfg.DEEPSEEK_BASE_URL.rstrip('/')}/user/balance",
                headers={"Authorization": f"Bearer {cfg.DEEPSEEK_API_KEY}"},
            )
            data = resp.json()
            try:
                bal = float(data["balance_infos"][0]["total_balance"])
                save_balance_snapshot(bal)
            except Exception:
                pass
            data["_stats"] = get_balance_stats()
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/admin/stats", summary="Token 统计")
async def get_stats():
    return JSONResponse(get_token_summary())


@router.get("/admin/models", summary="可用模型列表")
async def list_models():
    _FALLBACK = ["deepseek-v4-flash", "deepseek-v4-pro"]
    if not cfg.DEEPSEEK_API_KEY:
        return JSONResponse({"models": _FALLBACK, "source": "fallback"})
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.get(
                f"{cfg.DEEPSEEK_BASE_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {cfg.DEEPSEEK_API_KEY}"},
            )
            data = resp.json()
            models = [m["id"] for m in data.get("data", []) if m.get("id")]
            return JSONResponse({"models": models or _FALLBACK, "source": "api" if models else "fallback"})
    except Exception as e:
        logger.warning("获取模型列表失败: %s", e)
        return JSONResponse({"models": _FALLBACK, "source": "fallback"})


@router.get("/admin/model", summary="当前默认模型")
async def get_model():
    return JSONResponse({"model": cfg.DEEPSEEK_MODEL})


@router.post("/admin/model", summary="设置默认模型")
async def set_model(request: Request):
    try:
        body = await request.json()
        new_model = body.get("model", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")
    if not new_model:
        raise HTTPException(status_code=400, detail="model 不能为空")
    cfg.DEEPSEEK_MODEL = new_model
    logger.info("默认模型已更新为 %s", cfg.DEEPSEEK_MODEL)
    return JSONResponse({"ok": True, "model": cfg.DEEPSEEK_MODEL})
