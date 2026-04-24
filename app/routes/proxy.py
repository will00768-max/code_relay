"""
代理路由：
  POST /responses, /v1/responses          → DeepSeek chat/completions（Codex 格式转换）
  POST /chat/completions, /v1/chat/completions → 直接透传
"""
import json
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI

import app.config as cfg
from app.converters import build_chat_params, chat_completion_to_response, stream_response_events
from app.database import record_tokens

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_client() -> AsyncOpenAI:
    if not cfg.DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY 未配置")
    return AsyncOpenAI(api_key=cfg.DEEPSEEK_API_KEY, base_url=cfg.DEEPSEEK_BASE_URL)


def _verify_auth(authorization: str | None):
    if not cfg.PROXY_API_KEY:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    if authorization.removeprefix("Bearer ").strip() != cfg.PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


# ── /responses ──────────────────────────────────────────────────

@router.post("/responses", summary="代理 Codex /responses → DeepSeek")
@router.post("/v1/responses", summary="代理 Codex /v1/responses → DeepSeek")
async def proxy_responses(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _verify_auth(authorization)

    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")

    raw_model = body.get("model", cfg.DEEPSEEK_MODEL)
    req_model = cfg.resolve_model(raw_model)
    if req_model != raw_model:
        logger.info("模型名映射 %s → %s", raw_model, req_model)

    logger.info("收到请求 model=%s stream=%s", req_model, body.get("stream", False))
    logger.debug("请求体:\n%s", json.dumps(body, ensure_ascii=False, indent=2))

    client = _get_client()
    params = build_chat_params(body, req_model)
    is_stream = params.get("stream", False)
    response_id = f"resp_{uuid.uuid4().hex}"

    logger.debug("发送给 DeepSeek 的参数:\n%s", json.dumps(
        {k: v for k, v in params.items() if k != "messages"},
        ensure_ascii=False, indent=2,
    ))
    logger.debug("messages(%d 条):\n%s", len(params.get("messages", [])),
                 json.dumps(params.get("messages", []), ensure_ascii=False, indent=2))

    try:
        if is_stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}
            stream = await client.chat.completions.create(**params)
            return StreamingResponse(
                stream_response_events(stream, req_model, response_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            params["stream"] = False
            completion = await client.chat.completions.create(**params)
            result = chat_completion_to_response(completion, req_model)
            u = result.get("usage", {})
            record_tokens(req_model, u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("total_tokens", 0))
            logger.debug("非流式返回:\n%s", json.dumps(result, ensure_ascii=False, indent=2))
            return JSONResponse(content=result)
    except Exception as e:
        logger.exception("调用 DeepSeek API 失败: %s", e)
        raise HTTPException(status_code=502, detail=f"DeepSeek API 错误: {e}")


# ── /chat/completions 透传 ───────────────────────────────────────

@router.post("/chat/completions", summary="透传 /chat/completions → DeepSeek")
@router.post("/v1/chat/completions", summary="透传 /v1/chat/completions → DeepSeek")
async def proxy_chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _verify_auth(authorization)

    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")

    req_model = body.pop("model", cfg.DEEPSEEK_MODEL)
    is_stream = body.get("stream", False)
    logger.info("透传 chat/completions model=%s stream=%s", req_model, is_stream)

    client = _get_client()
    try:
        if is_stream:
            body["stream_options"] = {"include_usage": True}
            stream = await client.chat.completions.create(model=req_model, **body)

            async def passthrough():
                async for chunk in stream:
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                passthrough(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            completion = await client.chat.completions.create(model=req_model, **body)
            return JSONResponse(content=completion.model_dump())
    except Exception as e:
        logger.exception("调用 DeepSeek API 失败: %s", e)
        raise HTTPException(status_code=502, detail=f"DeepSeek API 错误: {e}")
