"""
Codex → DeepSeek 代理服务
将 OpenAI /v1/responses API 请求转换并代理到 DeepSeek /chat/completions API
"""

import json
import os
import time
import uuid
import logging
import sqlite3
import threading
from datetime import date, datetime
from typing import AsyncIterator

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from openai import AsyncOpenAI

load_dotenv()

# ──────────────────────────── 配置 ────────────────────────────

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")  # 留空则不校验
PORT = int(os.getenv("PORT", "8000"))

# 模型名映射表：将旧/别名模型名规范化为 DeepSeek 当前官方支持的模型名。
# 官方当前有效模型名：deepseek-v4-flash、deepseek-v4-pro
# deepseek-chat / deepseek-reasoner 为即将弃用的旧名称。
_MODEL_ALIAS: dict[str, str] = {
    # 新名称直接透传
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    # 旧名称 → 映射到对应新名称
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",  # thinking 模式由 deepseek-v4-flash 承担
    # 其他常见变体
    "deepseek-v3": "deepseek-v4-flash",
    "deepseek-v4": "deepseek-v4-flash",
    "deepseek-v4-5": "deepseek-v4-flash",
}

def resolve_model(name: str) -> str:
    """将任意模型名解析为 DeepSeek API 实际支持的模型名。"""
    lower = name.lower()
    # 精确匹配
    if lower in _MODEL_ALIAS:
        return _MODEL_ALIAS[lower]
    # 包含 pro → deepseek-v4-pro
    if "pro" in lower:
        return "deepseek-v4-pro"
    # 其余所有 deepseek-* → deepseek-v4-flash
    if lower.startswith("deepseek"):
        return "deepseek-v4-flash"
    # 非 deepseek 模型名（如 gpt-*、claude-* 等）→ 回退到默认模型
    return DEEPSEEK_MODEL

# ──────────────────────────── Token 统计（SQLite） ────────────────────────────

_DB_FILE = os.path.join(os.path.dirname(__file__), "stats.db")
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_stats (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT    NOT NULL,          -- ISO date string, e.g. 2026-04-24
                model     TEXT    NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON token_stats(ts)")
        # 余额快照表：每次主动查询余额时保存一条
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshot (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT    NOT NULL,           -- ISO datetime
                date      TEXT    NOT NULL,           -- ISO date, e.g. 2026-04-24
                balance   REAL    NOT NULL            -- 剩余余额（元）
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bdate ON balance_snapshot(date)")
        conn.commit()


def record_tokens(model: str, input_tokens: int, output_tokens: int, total_tokens: int):
    """记录一次请求的 token 消耗。"""
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO token_stats (ts, model, input_tokens, output_tokens, total_tokens) VALUES (?,?,?,?,?)",
                (today, model, input_tokens, output_tokens, total_tokens),
            )
            conn.commit()


def save_balance_snapshot(balance: float):
    """保存一条余额快照（每次查余额时调用）。"""
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO balance_snapshot (ts, date, balance) VALUES (?,?,?)",
                (now, today, balance),
            )
            conn.commit()


def get_balance_stats() -> dict:
    """
    从快照计算今日消费和总消费（差值法）。
    今日消费  = 今天最早快照余额 - 今天最新快照余额
    总消费    = 历史最早快照余额 - 最新快照余额
    """
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            latest = conn.execute(
                "SELECT balance FROM balance_snapshot ORDER BY id DESC LIMIT 1"
            ).fetchone()
            today_first = conn.execute(
                "SELECT balance FROM balance_snapshot WHERE date = ? ORDER BY id ASC LIMIT 1",
                (today,),
            ).fetchone()
            all_first = conn.execute(
                "SELECT balance FROM balance_snapshot ORDER BY id ASC LIMIT 1"
            ).fetchone()

    current = latest["balance"] if latest else None
    today_spent = round(today_first["balance"] - current, 6) if (today_first and current is not None) else None
    total_spent = round(all_first["balance"] - current, 6) if (all_first and current is not None) else None
    return {
        "current_balance": current,
        "today_spent":     today_spent,
        "total_spent":     total_spent,
    }


def get_token_summary() -> dict:
    """返回今日和总计的 token 使用量及估算消费金额。"""
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            row_today = conn.execute(
                "SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(total_tokens) as tot "
                "FROM token_stats WHERE ts = ? GROUP BY model",
                (today,),
            ).fetchall()
            row_total = conn.execute(
                "SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(total_tokens) as tot "
                "FROM token_stats GROUP BY model",
            ).fetchall()

    # 单价表（元/百万tokens），取非缓存命中（偏保守）
    _PRICE: dict[str, tuple[float, float]] = {
        "deepseek-v4-flash":   (1.0,  2.0),   # (输入, 输出)
        "deepseek-v4-pro":     (12.0, 24.0),
        "deepseek-chat":       (1.0,  2.0),
        "deepseek-reasoner":   (4.0,  16.0),
    }
    _DEFAULT_PRICE = (1.0, 2.0)

    def calc_cost(rows) -> tuple[int, int, int, float]:
        inp = out = tot = 0
        cost = 0.0
        for r in rows:
            m = (r["model"] or "").lower()
            pin, pout = _PRICE.get(m, _DEFAULT_PRICE)
            inp  += r["inp"] or 0
            out  += r["out"] or 0
            tot  += r["tot"] or 0
            cost += (r["inp"] or 0) / 1_000_000 * pin
            cost += (r["out"] or 0) / 1_000_000 * pout
        return inp, out, tot, cost

    ti, to, tt, tc = calc_cost(row_today)
    ai, ao, at_, ac = calc_cost(row_total)
    return {
        "today": {
            "input_tokens":  ti,
            "output_tokens": to,
            "total_tokens":  tt,
            "cost_cny":      round(tc, 6),
        },
        "total": {
            "input_tokens":  ai,
            "output_tokens": ao,
            "total_tokens":  at_,
            "cost_cny":      round(ac, 6),
        },
    }


_init_db()

LOG_FILE = os.path.join(os.path.dirname(__file__), "relay.log")

# 同时输出到控制台和文件
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_fmt)
_file_handler.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)
_console_handler.setLevel(logging.INFO)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[_console_handler])
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(_file_handler)
logger.propagate = False
logger.setLevel(logging.DEBUG)

# ──────────────────────────── FastAPI App ────────────────────────────

app = FastAPI(
    title="Codex → DeepSeek 代理",
    description="将 OpenAI /v1/responses 请求代理到 DeepSeek /chat/completions",
    version="1.0.0",
)

# ──────────────────────────── 工具函数 ────────────────────────────

def get_deepseek_client() -> AsyncOpenAI:
    """创建 DeepSeek OpenAI 兼容客户端"""
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY 未配置")
    return AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def verify_auth(authorization: str | None):
    """校验代理鉴权（若 PROXY_API_KEY 已配置）"""
    if not PROXY_API_KEY:
        return  # 未设置则跳过
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    token = authorization.removeprefix("Bearer ").strip()
    if token != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


def _extract_text_content(content) -> str:
    """将 content blocks 列表或字符串统一转为纯文本字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype in ("text", "input_text", "output_text"):
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def responses_input_to_messages(input_data) -> list[dict]:
    """
    将 /v1/responses 的 input 字段转换为 chat/completions 的 messages 列表。

    Codex Responses API 的 input 是一个扁平条目列表，条目类型有：
      - type="message"        → 普通消息（role: system/user/assistant/developer/...）
      - type="function_call"  → 模型发起的工具调用，需合并为 assistant.tool_calls
      - type="function_call_output" → 工具执行结果，转为 role="tool" 消息
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if not isinstance(input_data, list):
        return [{"role": "user", "content": str(input_data)}]

    # DeepSeek 支持的合法 role
    VALID_ROLES = {"system", "user", "assistant", "tool"}
    ROLE_MAP = {
        "developer": "system",
        "latest_reminder": "system",
        "function": "assistant",
    }

    messages: list[dict] = []

    def last_assistant() -> dict | None:
        """返回 messages 末尾的 assistant 消息（如果存在）。"""
        if messages and messages[-1]["role"] == "assistant":
            return messages[-1]
        return None

    for item in input_data:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue

        if not isinstance(item, dict):
            continue

        item_type = item.get("type", "")

        # ── function_call：模型调用工具的记录 → 合并到 assistant.tool_calls ──
        if item_type == "function_call":
            call_id = item.get("call_id", item.get("id", f"call_{uuid.uuid4().hex[:16]}"))
            func_name = item.get("name", "")
            func_args = item.get("arguments", "")
            tool_call_entry = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": func_args if isinstance(func_args, str) else json.dumps(func_args, ensure_ascii=False),
                },
            }
            asst = last_assistant()
            if asst is not None:
                # 追加到已有 assistant 消息的 tool_calls
                asst.setdefault("tool_calls", []).append(tool_call_entry)
            else:
                # 没有 assistant 消息，新建一条
                # DeepSeek 要求 content 为 null（不能是空字符串）
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call_entry],
                })
            continue

        # ── function_call_output：工具执行结果 → role="tool" ──
        if item_type == "function_call_output":
            raw_output = item.get("output", "")
            # Codex 的 output 可能是 JSON 字符串：{"output":"...", "metadata":{...}}
            # DeepSeek tool content 需要是实际结果字符串，提取 output 字段
            if isinstance(raw_output, str):
                try:
                    parsed = json.loads(raw_output)
                    if isinstance(parsed, dict) and "output" in parsed:
                        tool_content = str(parsed["output"])
                    else:
                        tool_content = raw_output
                except (json.JSONDecodeError, ValueError):
                    tool_content = raw_output
            else:
                tool_content = json.dumps(raw_output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", item.get("id", "")),
                "content": tool_content,
            })
            continue

        # ── 其他非 message 类型跳过 ──
        if item_type and item_type != "message":
            continue

        # ── 普通 message 条目 ──
        role = item.get("role", "user")
        role = ROLE_MAP.get(role, role)
        if role not in VALID_ROLES:
            role = "user"

        content = _extract_text_content(item.get("content", ""))
        messages.append({"role": role, "content": content})

    return messages


def convert_tools(tools: list) -> list:
    """
    将 Codex Responses API 的 tools 格式转为 DeepSeek chat/completions 格式。

    Codex 可能传入两种形式：
    1. 扁平格式（Responses API）：
       {"type": "function", "name": "...", "description": "...", "parameters": {...}}
    2. 已是标准格式：
       {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type", "function")

        # 已是标准嵌套格式，直接保留
        if "function" in tool:
            converted.append({"type": "function", "function": tool["function"]})
            continue

        # 扁平格式 → 转为嵌套格式
        func: dict = {}
        if "name" in tool:
            func["name"] = tool["name"]
        if "description" in tool:
            func["description"] = tool["description"]
        if "parameters" in tool:
            func["parameters"] = tool["parameters"]
        elif "input_schema" in tool:
            # Anthropic 风格的 input_schema 也做兼容
            func["parameters"] = tool["input_schema"]

        if func.get("name"):
            converted.append({"type": "function", "function": func})

    return converted


def build_chat_params(body: dict, model: str) -> dict:
    """从 /v1/responses 请求体构建 chat/completions 参数"""
    params: dict = {"model": model}

    # ── messages ──
    input_data = body.get("input", body.get("messages", ""))
    messages = responses_input_to_messages(input_data)

    # system prompt（Responses API 顶层字段）
    system_prompt = body.get("instructions") or body.get("system")
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    # ── 清理 reasoning_content ──
    # DeepSeek 官方文档明确：多轮对话拼接 assistant 历史消息时，
    # 必须删除 reasoning_content，否则返回 400 错误。
    # （thinking 模型的内部思考过程不应作为上下文回传）
    for msg in messages:
        msg.pop("reasoning_content", None)

    params["messages"] = messages

    # ── 常用参数直接透传 ──
    for key in ("temperature", "top_p", "max_tokens", "stop",
                "frequency_penalty", "presence_penalty",
                "logprobs", "top_logprobs", "stream"):
        if key in body:
            params[key] = body[key]

    # ── max_output_tokens → max_tokens ──
    if "max_output_tokens" in body and "max_tokens" not in params:
        params["max_tokens"] = body["max_output_tokens"]

    # ── tools / tool_choice ──
    if "tools" in body:
        params["tools"] = convert_tools(body["tools"])
    if "tool_choice" in body:
        params["tool_choice"] = body["tool_choice"]

    # ── response_format ──
    if "response_format" in body:
        params["response_format"] = body["response_format"]
    elif body.get("text", {}).get("format", {}).get("type") == "json_object":
        params["response_format"] = {"type": "json_object"}

    # ── stream ──
    params.setdefault("stream", False)

    return params


def chat_completion_to_response(completion, model: str) -> dict:
    """
    将 DeepSeek chat.completion 对象转换为 OpenAI /v1/responses 格式响应
    """
    choice = completion.choices[0]
    message = choice.message
    content_text = message.content or ""

    # output 数组
    output = [
        {
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:20]}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content_text, "annotations": []}],
        }
    ]

    # tool_calls → function_call output（如有）
    if message.tool_calls:
        for tc in message.tool_calls:
            output.append(
                {
                    "type": "function_call",
                    "id": tc.id,
                    "call_id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                    "status": "completed",
                }
            )

    usage = completion.usage
    return {
        "id": completion.id or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": completion.created or int(time.time()),
        "status": "completed",
        "model": completion.model or model,
        "output": output,
        "usage": {
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
        },
        # 保留原始字段方便调试
        "_deepseek_finish_reason": choice.finish_reason,
    }


async def stream_response_events(
    stream, model: str, response_id: str
) -> AsyncIterator[str]:
    """
    将 DeepSeek 流式 chunks 转换为 OpenAI /v1/responses SSE 事件格式。
    同时正确处理：
      - 普通文本 delta
      - tool_calls delta（按 index 拼接 arguments）
    """

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # ── 固定头部事件 ──
    yield sse("response.created", {
        "type": "response.created",
        "response": {"id": response_id, "object": "response",
                     "status": "in_progress", "model": model, "output": []},
    })
    yield sse("response.in_progress", {
        "type": "response.in_progress",
        "response": {"id": response_id},
    })

    # ── 消息 item（文本输出用） ──
    msg_item_id = f"msg_{uuid.uuid4().hex[:20]}"
    msg_item_announced = False   # 延迟到确认有文本内容时才宣告
    full_text = ""

    # ── tool_calls 拼接结构 ──
    # { index: { id, type, name, arguments, item_id, output_index, announced } }
    tool_calls_buf: dict[int, dict] = {}
    # 当前已使用的 output_index（文本消息占 0，tool call 从 1 开始）
    next_output_index = 1

    usage_data = None

    async for chunk in stream:
        if not chunk.choices:
            if getattr(chunk, "usage", None):
                usage_data = chunk.usage
            continue

        choice = chunk.choices[0]
        delta = choice.delta

        # ────── 文本 delta ──────
        delta_content = delta.content or ""
        if delta_content:
            # 首次有文本，先宣告 message item 和 content_part
            if not msg_item_announced:
                msg_item_announced = True
                yield sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": msg_item_id, "object": "realtime.item",
                             "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []},
                })
                yield sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": msg_item_id,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                })

            full_text += delta_content
            yield sse("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": msg_item_id,
                "output_index": 0, "content_index": 0,
                "delta": delta_content,
            })

        # ────── tool_calls delta（按文档：index 标识、首帧含 id/name、后续只有 arguments 增量） ──────
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index

                if idx not in tool_calls_buf:
                    # 首帧：初始化，分配 output_index
                    call_item_id = f"fc_{uuid.uuid4().hex[:20]}"
                    tool_calls_buf[idx] = {
                        "id": tc.id or "",
                        "type": "function",
                        "name": tc.function.name if tc.function else "",
                        "arguments": "",
                        "item_id": call_item_id,
                        "output_index": next_output_index,
                        "announced": False,
                    }
                    next_output_index += 1

                buf = tool_calls_buf[idx]

                # 补充首帧字段（有时 id/name 分多帧到达）
                if tc.id:
                    buf["id"] = tc.id
                if tc.function and tc.function.name:
                    buf["name"] = tc.function.name

                # 拼接 arguments 增量
                if tc.function and tc.function.arguments:
                    buf["arguments"] += tc.function.arguments

                # 当 name 已知且未宣告时，发出 output_item.added
                if buf["name"] and not buf["announced"]:
                    buf["announced"] = True
                    yield sse("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": buf["output_index"],
                        "item": {
                            "id": buf["item_id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": buf["id"],
                            "name": buf["name"],
                            "arguments": "",
                        },
                    })

                # 每次 arguments 有增量都发 delta 事件
                if tc.function and tc.function.arguments:
                    yield sse("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": buf["item_id"],
                        "output_index": buf["output_index"],
                        "delta": tc.function.arguments,
                    })

        if choice.finish_reason:
            if getattr(chunk, "usage", None):
                usage_data = chunk.usage

    # ──────────── 收尾事件 ────────────

    # 文本消息收尾
    if msg_item_announced:
        yield sse("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_item_id,
            "output_index": 0, "content_index": 0,
            "text": full_text,
        })
        yield sse("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_item_id,
            "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": full_text},
        })
        yield sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": msg_item_id, "object": "realtime.item",
                "type": "message", "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text, "annotations": []}],
            },
        })

    # tool_calls 收尾
    final_output = []
    if msg_item_announced:
        final_output.append({
            "type": "message", "id": msg_item_id,
            "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": full_text, "annotations": []}],
        })

    for idx in sorted(tool_calls_buf):
        buf = tool_calls_buf[idx]
        # arguments done
        yield sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "item_id": buf["item_id"],
            "output_index": buf["output_index"],
            "arguments": buf["arguments"],
        })
        # output_item.done for tool call
        yield sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": buf["output_index"],
            "item": {
                "id": buf["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": buf["id"],
                "name": buf["name"],
                "arguments": buf["arguments"],
            },
        })
        final_output.append({
            "type": "function_call",
            "id": buf["item_id"],
            "call_id": buf["id"],
            "name": buf["name"],
            "arguments": buf["arguments"],
            "status": "completed",
        })

    # response.completed
    yield sse("response.completed", {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "model": model,
            "output": final_output,
            "usage": {
                "input_tokens": usage_data.prompt_tokens if usage_data else 0,
                "output_tokens": usage_data.completion_tokens if usage_data else 0,
                "total_tokens": usage_data.total_tokens if usage_data else 0,
            },
        },
    })

    logger.debug(
        "流式响应汇总 response_id=%s text_len=%d tool_calls=%d input_tokens=%d output_tokens=%d",
        response_id,
        len(full_text),
        len(tool_calls_buf),
        usage_data.prompt_tokens if usage_data else 0,
        usage_data.completion_tokens if usage_data else 0,
    )
    # 记录 token 消耗
    record_tokens(
        model,
        usage_data.prompt_tokens if usage_data else 0,
        usage_data.completion_tokens if usage_data else 0,
        usage_data.total_tokens if usage_data else 0,
    )
    if full_text:
        logger.debug("流式完整文本:\n%s", full_text)
    if tool_calls_buf:
        logger.debug("流式 tool_calls:\n%s", json.dumps(
            [{
                "index": k,
                "id": v["id"],
                "name": v["name"],
                "arguments": v["arguments"],
            } for k, v in sorted(tool_calls_buf.items())],
            ensure_ascii=False, indent=2,
        ))

    yield "data: [DONE]\n\n"


# ──────────────────────────── 路由 ────────────────────────────


@app.get("/", summary="健康检查")
async def health():
    return {"status": "ok", "service": "codex-deepseek-proxy"}


@app.get("/admin", response_class=HTMLResponse, summary="管理面板")
async def admin_panel():
    html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeepSeek 代理管理面板</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3e;
    --accent: #4f8ef7; --green: #22c55e; --red: #ef4444;
    --text: #e2e8f0; --muted: #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; padding: 24px; }
  h1 { font-size: 1.5rem; font-weight: 700; margin-bottom: 24px; display: flex; align-items: center; gap: 10px; }
  h1 span.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); display: inline-block; box-shadow: 0 0 8px var(--green); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
  .card h2 { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 12px; }
  .stat-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
  .stat-row:last-child { border-bottom: none; }
  .stat-val { font-weight: 600; color: var(--accent); font-size: 1rem; }
  .balance-val { font-size: 1.6rem; font-weight: 700; color: var(--green); }
  .balance-used { font-size: 1rem; font-weight: 600; color: var(--red); }
  label { display: block; font-size: 0.82rem; color: var(--muted); margin-bottom: 6px; margin-top: 14px; }
  input, select {
    width: 100%; background: var(--bg); border: 1px solid var(--border);
    color: var(--text); border-radius: 8px; padding: 8px 12px; font-size: 0.9rem;
    outline: none; transition: border .2s;
  }
  input:focus, select:focus { border-color: var(--accent); }
  .btn {
    margin-top: 16px; width: 100%; background: var(--accent); color: #fff;
    border: none; border-radius: 8px; padding: 9px; font-size: 0.9rem;
    cursor: pointer; font-weight: 600; transition: opacity .2s;
  }
  .btn:hover { opacity: .85; }
  .btn.danger { background: var(--red); }
  .msg { margin-top: 10px; font-size: 0.82rem; min-height: 18px; }
  .msg.ok { color: var(--green); }
  .msg.err { color: var(--red); }
  .refresh-btn { background: none; border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 4px 10px; font-size: 0.78rem; cursor: pointer; float: right; }
  .refresh-btn:hover { border-color: var(--accent); color: var(--accent); }
  .ts { font-size: 0.75rem; color: var(--muted); margin-top: 8px; text-align: right; }
</style>
</head>
<body>
<h1><span class="dot"></span> DeepSeek 代理管理面板</h1>

<div class="grid">
  <!-- 余额卡片 -->
  <div class="card">
    <h2>账户余额 <button class="refresh-btn" onclick="loadBalance()">刷新</button></h2>
    <div class="stat-row"><span>剩余余额</span><span class="balance-val" id="bal-remain">--</span></div>
    <div class="stat-row"><span>今日消费</span><span class="balance-used" id="bal-today">--</span></div>
    <div class="stat-row"><span>累计消费</span><span class="balance-used" id="bal-total">--</span></div>
    <div class="ts" id="bal-ts">首次刷新后记录基准快照，之后每次刷新计算差值</div>
  </div>

  <!-- 今日 Token -->
  <div class="card">
    <h2>今日 Token 用量 <button class="refresh-btn" onclick="loadStats()">刷新</button></h2>
    <div class="stat-row"><span>输入 Tokens</span><span class="stat-val" id="td-in">--</span></div>
    <div class="stat-row"><span>输出 Tokens</span><span class="stat-val" id="td-out">--</span></div>
    <div class="stat-row"><span>合计 Tokens</span><span class="stat-val" id="td-tot">--</span></div>
    <div class="stat-row"><span>估算消费</span><span class="balance-used" id="td-cost">--</span></div>
  </div>

  <!-- 总计 Token -->
  <div class="card">
    <h2>累计 Token 用量</h2>
    <div class="stat-row"><span>输入 Tokens</span><span class="stat-val" id="tt-in">--</span></div>
    <div class="stat-row"><span>输出 Tokens</span><span class="stat-val" id="tt-out">--</span></div>
    <div class="stat-row"><span>合计 Tokens</span><span class="stat-val" id="tt-tot">--</span></div>
    <div class="stat-row"><span>估算消费</span><span class="balance-used" id="tt-cost">--</span></div>
  </div>

  <!-- 模型配置 -->
  <div class="card">
    <h2>模型配置 <button class="refresh-btn" onclick="loadModel()">刷新列表</button></h2>
    <label>当前默认模型（所有 Codex 请求将强制使用此模型）</label>
    <select id="model-select">
      <option value="">加载中…</option>
    </select>
    <label>自定义模型名（填写后覆盖上方选择）</label>
    <input type="text" id="model-custom" placeholder="例如: deepseek-v4-flash">
    <button class="btn" onclick="saveModel()">保存模型配置</button>
    <div class="msg" id="model-msg"></div>
  </div>
</div>

<script>
async function loadBalance() {
  document.getElementById('bal-ts').textContent = '加载中…';
  try {
    const r = await fetch('/admin/balance');
    const d = await r.json();
    if (d.error) { document.getElementById('bal-remain').textContent = '查询失败'; document.getElementById('bal-ts').textContent = d.error; return; }
    const bal = d.balance_infos?.[0]?.total_balance;
    document.getElementById('bal-remain').textContent = bal != null ? '¥' + Number(bal).toFixed(4) : '--';
    const s = d._stats || {};
    document.getElementById('bal-today').textContent = s.today_spent != null ? '¥' + s.today_spent.toFixed(4) : '（需2次快照）';
    document.getElementById('bal-total').textContent = s.total_spent != null ? '¥' + s.total_spent.toFixed(4) : '（需2次快照）';
    document.getElementById('bal-ts').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN') + '（每次刷新存快照，差值=上次余额-当前余额）';
  } catch(e) { document.getElementById('bal-ts').textContent = '请求失败: ' + e.message; }
}

async function loadStats() {
  try {
    const r = await fetch('/admin/stats');
    const d = await r.json();
    const fmt = n => n.toLocaleString();
    document.getElementById('td-in').textContent  = fmt(d.today.input_tokens);
    document.getElementById('td-out').textContent = fmt(d.today.output_tokens);
    document.getElementById('td-tot').textContent = fmt(d.today.total_tokens);
    document.getElementById('td-cost').textContent = '≈ ¥' + d.today.cost_cny.toFixed(4);
    document.getElementById('tt-in').textContent  = fmt(d.total.input_tokens);
    document.getElementById('tt-out').textContent = fmt(d.total.output_tokens);
    document.getElementById('tt-tot').textContent = fmt(d.total.total_tokens);
    document.getElementById('tt-cost').textContent = '≈ ¥' + d.total.cost_cny.toFixed(4);
  } catch(e) { console.error(e); }
}

async function loadModel() {
  const sel = document.getElementById('model-select');
  try {
    // 同时拉模型列表和当前选中模型
    const [rList, rCur] = await Promise.all([fetch('/admin/models'), fetch('/admin/model')]);
    const dList = await rList.json();
    const dCur  = await rCur.json();
    const current = dCur.model || '';

    // 重建 <option>
    sel.innerHTML = '';
    const models = dList.models || [];
    models.forEach(id => {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = id;
      sel.appendChild(opt);
    });

    // 如果当前模型不在列表里，额外插一条并选中
    if (!models.includes(current) && current) {
      const opt = document.createElement('option');
      opt.value = current;
      opt.textContent = current + '（当前）';
      sel.insertBefore(opt, sel.firstChild);
    }

    if (models.includes(current)) {
      sel.value = current;
      document.getElementById('model-custom').value = '';
    } else {
      sel.value = models[0] || '';
      document.getElementById('model-custom').value = current;
    }

    const src = dList.source === 'fallback' ? '（API 不可用，显示默认列表）' : '';
    document.getElementById('model-msg').textContent = src;
    document.getElementById('model-msg').className = src ? 'msg' : '';
  } catch(e) { sel.innerHTML = '<option value="">加载失败</option>'; }
}

async function saveModel() {
  const custom = document.getElementById('model-custom').value.trim();
  const model = custom || document.getElementById('model-select').value;
  const msg = document.getElementById('model-msg');
  try {
    const r = await fetch('/admin/model', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({model}) });
    const d = await r.json();
    if (d.ok) { msg.textContent = '✓ 已更新为 ' + d.model; msg.className = 'msg ok'; }
    else { msg.textContent = '失败: ' + JSON.stringify(d); msg.className = 'msg err'; }
  } catch(e) { msg.textContent = '请求失败'; msg.className = 'msg err'; }
}

loadBalance(); loadStats(); loadModel();
setInterval(() => { loadStats(); }, 30000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/admin/balance", summary="查询 DeepSeek 账户余额")
async def get_balance():
    """调用 DeepSeek /user/balance 接口返回余额信息，并自动保存快照用于消费差值计算。"""
    import httpx
    if not DEEPSEEK_API_KEY:
        return JSONResponse({"error": "DEEPSEEK_API_KEY 未配置"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.get(
                f"{DEEPSEEK_BASE_URL.rstrip('/')}/user/balance",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            )
            data = resp.json()
            # 保存快照
            try:
                bal = float(data["balance_infos"][0]["total_balance"])
                save_balance_snapshot(bal)
            except Exception:
                pass
            # 附加差值统计
            data["_stats"] = get_balance_stats()
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/admin/stats", summary="查询 token 统计")
async def get_stats():
    return JSONResponse(get_token_summary())


@app.get("/admin/models", summary="从 DeepSeek 获取可用模型列表")
async def list_models():
    """代理 DeepSeek GET /models 接口，失败时返回内置默认列表。"""
    import httpx
    _FALLBACK = ["deepseek-v4-flash", "deepseek-v4-pro"]
    if not DEEPSEEK_API_KEY:
        return JSONResponse({"models": _FALLBACK, "source": "fallback"})
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.get(
                f"{DEEPSEEK_BASE_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            )
            data = resp.json()
            models = [m["id"] for m in data.get("data", []) if m.get("id")]
            if not models:
                models = _FALLBACK
            return JSONResponse({"models": models, "source": "api"})
    except Exception as e:
        logger.warning("获取模型列表失败: %s", e)
        return JSONResponse({"models": _FALLBACK, "source": "fallback"})


@app.get("/admin/model", summary="查询当前默认模型")
async def get_model():
    return JSONResponse({"model": DEEPSEEK_MODEL})


@app.post("/admin/model", summary="设置默认模型")
async def set_model(request: Request):
    global DEEPSEEK_MODEL
    try:
        body = await request.json()
        new_model = body.get("model", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")
    if not new_model:
        raise HTTPException(status_code=400, detail="model 不能为空")
    DEEPSEEK_MODEL = new_model
    logger.info("默认模型已更新为 %s", DEEPSEEK_MODEL)
    return JSONResponse({"ok": True, "model": DEEPSEEK_MODEL})


@app.post("/responses", summary="代理 Codex /responses → DeepSeek /chat/completions")
@app.post("/v1/responses", summary="代理 Codex /v1/responses → DeepSeek /chat/completions")
async def proxy_responses(
    request: Request,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)

    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")

    # 目标模型：优先使用请求中指定的，否则用配置默认值；统一映射为合法模型名
    raw_model = body.get("model", DEEPSEEK_MODEL)
    req_model = resolve_model(raw_model)
    if req_model != raw_model:
        logger.info("模型名映射 %s → %s", raw_model, req_model)

    logger.info("收到请求 model=%s stream=%s", req_model, body.get("stream", False))
    logger.debug("请求体:\n%s", json.dumps(body, ensure_ascii=False, indent=2))

    client = get_deepseek_client()
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
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            params["stream"] = False
            completion = await client.chat.completions.create(**params)
            result = chat_completion_to_response(completion, req_model)
            # 记录 token
            u = result.get("usage", {})
            record_tokens(req_model, u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("total_tokens", 0))
            logger.debug("DeepSeek 非流式返回:\n%s", json.dumps(result, ensure_ascii=False, indent=2))
            return JSONResponse(content=result)

    except Exception as e:
        logger.exception("调用 DeepSeek API 失败: %s", e)
        raise HTTPException(status_code=502, detail=f"DeepSeek API 错误: {e}")


@app.post("/chat/completions", summary="直接透传 /chat/completions → DeepSeek（兼容标准 OpenAI 客户端）")
@app.post(
    "/v1/chat/completions",
    summary="直接透传 /v1/chat/completions → DeepSeek（兼容标准 OpenAI 客户端）",
)
async def proxy_chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """原样透传 chat/completions，仅替换 API Key 和 Base URL"""
    verify_auth(authorization)

    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")

    req_model = body.pop("model", DEEPSEEK_MODEL)
    is_stream = body.get("stream", False)
    logger.info("透传 chat/completions model=%s stream=%s", req_model, is_stream)

    client = get_deepseek_client()

    try:
        if is_stream:
            body["stream_options"] = {"include_usage": True}
            stream = await client.chat.completions.create(model=req_model, **body)

            async def passthrough_stream():
                async for chunk in stream:
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                passthrough_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            completion = await client.chat.completions.create(model=req_model, **body)
            return JSONResponse(content=completion.model_dump())

    except Exception as e:
        logger.exception("调用 DeepSeek API 失败: %s", e)
        raise HTTPException(status_code=502, detail=f"DeepSeek API 错误: {e}")


# ──────────────────────────── 入口 ────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
