"""
请求/响应格式转换：
  - Codex /v1/responses input  →  DeepSeek chat/completions messages
  - DeepSeek chat.completion    →  OpenAI /v1/responses 响应体
  - DeepSeek 流式 chunks        →  SSE 事件流
"""
import json
import time
import uuid
import logging
from typing import AsyncIterator

from app.database import record_tokens

logger = logging.getLogger(__name__)


# ─────────────────────── 工具转换 ───────────────────────

def convert_tools(tools: list) -> list:
    """
    Codex Responses API tools → DeepSeek chat/completions tools。
    支持扁平格式和已嵌套格式。
    """
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "function" in tool:
            converted.append({"type": "function", "function": tool["function"]})
            continue
        func: dict = {}
        if "name" in tool:
            func["name"] = tool["name"]
        if "description" in tool:
            func["description"] = tool["description"]
        if "parameters" in tool:
            func["parameters"] = tool["parameters"]
        elif "input_schema" in tool:
            func["parameters"] = tool["input_schema"]
        if func.get("name"):
            converted.append({"type": "function", "function": func})
    return converted


# ─────────────────────── content 文本提取 ───────────────────────

def _extract_text_content(content) -> str:
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


# ─────────────────────── input → messages ───────────────────────

def responses_input_to_messages(input_data) -> list[dict]:
    """
    将 /v1/responses 的 input 转换为 chat/completions messages 列表。
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if not isinstance(input_data, list):
        return [{"role": "user", "content": str(input_data)}]

    VALID_ROLES = {"system", "user", "assistant", "tool"}
    ROLE_MAP = {
        "developer":        "system",
        "latest_reminder":  "system",
        "function":         "assistant",
    }

    messages: list[dict] = []

    def last_assistant() -> dict | None:
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

        # function_call → assistant.tool_calls
        if item_type == "function_call":
            call_id = item.get("call_id", item.get("id", f"call_{uuid.uuid4().hex[:16]}"))
            tool_call_entry = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": (
                        item["arguments"] if isinstance(item.get("arguments"), str)
                        else json.dumps(item.get("arguments", ""), ensure_ascii=False)
                    ),
                },
            }
            asst = last_assistant()
            if asst is not None:
                asst.setdefault("tool_calls", []).append(tool_call_entry)
                # 确保已有 assistant 消息也带 reasoning_content
                asst.setdefault("reasoning_content", "")
            else:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "",
                    "tool_calls": [tool_call_entry],
                })
            continue

        # function_call_output → role="tool"
        if item_type == "function_call_output":
            raw_output = item.get("output", "")
            if isinstance(raw_output, str):
                try:
                    parsed = json.loads(raw_output)
                    tool_content = str(parsed["output"]) if isinstance(parsed, dict) and "output" in parsed else raw_output
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

        if item_type and item_type != "message":
            continue

        # 普通 message
        role = ROLE_MAP.get(item.get("role", "user"), item.get("role", "user"))
        if role not in VALID_ROLES:
            role = "user"

        raw_content = item.get("content", "")

        # assistant 消息：分离 reasoning block 和文本
        if role == "assistant" and isinstance(raw_content, list):
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            for block in raw_content:
                if not isinstance(block, dict):
                    if isinstance(block, str):
                        text_parts.append(block)
                    continue
                btype = block.get("type", "")
                if btype in ("text", "output_text"):
                    text_parts.append(block.get("text", ""))
                elif btype == "reasoning":
                    # 优先取明文 text/summary，encrypted_content 不透传
                    r_text = block.get("text") or ""
                    if not r_text:
                        summary = block.get("summary")
                        if isinstance(summary, list):
                            for s in summary:
                                if isinstance(s, dict):
                                    reasoning_parts.append(s.get("text", ""))
                                elif isinstance(s, str):
                                    reasoning_parts.append(s)
                        elif isinstance(summary, str):
                            reasoning_parts.append(summary)
                    else:
                        reasoning_parts.append(r_text)
            msg: dict = {
                "role": "assistant",
                "content": "\n".join(text_parts),
                # deepseek-v4-flash thinking 模式要求 assistant 历史消息必须携带该字段
                "reasoning_content": "\n".join(reasoning_parts),
            }
            messages.append(msg)
        elif role == "assistant":
            # 非列表 content 的 assistant 消息（含 tool_calls 情况由 function_call 分支处理）
            # 补充空 reasoning_content 以满足 deepseek-v4-flash thinking 模式要求
            messages.append({
                "role": "assistant",
                "content": _extract_text_content(raw_content),
                "reasoning_content": "",
            })
        else:
            # user/system/tool 消息：保留图片块，构建多模态 content 数组
            if isinstance(raw_content, list):
                content_parts = []
                for block in raw_content:
                    if not isinstance(block, dict):
                        if isinstance(block, str) and block:
                            content_parts.append({"type": "text", "text": block})
                        continue
                    btype = block.get("type", "")
                    if btype in ("text", "input_text", "output_text"):
                        text = block.get("text", "")
                        if text:
                            content_parts.append({"type": "text", "text": text})
                    elif btype == "image_url":
                        content_parts.append(block)
                    elif btype == "image":
                        # Codex 格式：{ type: "image", source: { type: "url"/"base64", url/data } }
                        source = block.get("source", {})
                        src_type = source.get("type", "")
                        if src_type == "url":
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": source.get("url", "")},
                            })
                        elif src_type == "base64":
                            media_type = source.get("media_type", "image/jpeg")
                            data = source.get("data", "")
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"},
                            })
                # 若只有一个文本块，降级为字符串（兼容性更好）
                if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                    messages.append({"role": role, "content": content_parts[0]["text"]})
                elif content_parts:
                    messages.append({"role": role, "content": content_parts})
                else:
                    messages.append({"role": role, "content": ""})
            else:
                messages.append({"role": role, "content": _extract_text_content(raw_content)})

    return messages


# ─────────────────────── 构建 chat 请求参数 ───────────────────────

def build_chat_params(body: dict, model: str) -> dict:
    params: dict = {"model": model}

    input_data = body.get("input", body.get("messages", ""))
    messages = responses_input_to_messages(input_data)

    system_prompt = body.get("instructions") or body.get("system")
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    params["messages"] = messages

    for key in ("temperature", "top_p", "max_tokens", "stop",
                "frequency_penalty", "presence_penalty",
                "logprobs", "top_logprobs", "stream"):
        if key in body:
            params[key] = body[key]

    if "max_output_tokens" in body and "max_tokens" not in params:
        params["max_tokens"] = body["max_output_tokens"]

    if "tools" in body:
        params["tools"] = convert_tools(body["tools"])
    if "tool_choice" in body:
        params["tool_choice"] = body["tool_choice"]

    if "response_format" in body:
        params["response_format"] = body["response_format"]
    elif body.get("text", {}).get("format", {}).get("type") == "json_object":
        params["response_format"] = {"type": "json_object"}

    params.setdefault("stream", False)
    return params


# ─────────────────────── 非流式转换 ───────────────────────

def chat_completion_to_response(completion, model: str) -> dict:
    choice = completion.choices[0]
    message = choice.message
    content_text = message.content or ""

    content_blocks: list[dict] = []
    reasoning_text = getattr(message, "reasoning_content", None)
    if reasoning_text:
        content_blocks.append({
            "type": "reasoning",
            "text": reasoning_text,
            "summary": [{"type": "summary_text", "text": reasoning_text}],
        })
    content_blocks.append({"type": "output_text", "text": content_text, "annotations": []})

    output: list[dict] = [{
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:20]}",
        "status": "completed",
        "role": "assistant",
        "content": content_blocks,
    }]

    if message.tool_calls:
        for tc in message.tool_calls:
            output.append({
                "type": "function_call",
                "id": tc.id,
                "call_id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
                "status": "completed",
            })

    usage = completion.usage
    details = getattr(usage, "prompt_tokens_details", None) if usage else None
    cache_hit  = getattr(details, "cached_tokens", 0) or 0
    cache_miss = (usage.prompt_tokens - cache_hit) if usage else 0
    return {
        "id": completion.id or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": completion.created or int(time.time()),
        "status": "completed",
        "model": completion.model or model,
        "output": output,
        "usage": {
            "input_tokens":             usage.prompt_tokens if usage else 0,
            "input_cache_hit_tokens":   cache_hit,
            "input_cache_miss_tokens":  max(0, cache_miss),
            "output_tokens":            usage.completion_tokens if usage else 0,
            "total_tokens":             usage.total_tokens if usage else 0,
        },
        "_deepseek_finish_reason": choice.finish_reason,
    }


# ─────────────────────── 流式转换 ───────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_response_events(
    stream, model: str, response_id: str
) -> AsyncIterator[str]:
    yield _sse("response.created", {
        "type": "response.created",
        "response": {"id": response_id, "object": "response",
                     "status": "in_progress", "model": model, "output": []},
    })
    yield _sse("response.in_progress", {
        "type": "response.in_progress",
        "response": {"id": response_id},
    })

    msg_item_id = f"msg_{uuid.uuid4().hex[:20]}"
    msg_item_announced = False
    full_text = ""
    full_reasoning = ""

    tool_calls_buf: dict[int, dict] = {}
    next_output_index = 1
    usage_data = None

    async for chunk in stream:
        if not chunk.choices:
            if getattr(chunk, "usage", None):
                usage_data = chunk.usage
            continue

        choice = chunk.choices[0]
        delta = choice.delta

        # reasoning_content delta
        delta_reasoning = getattr(delta, "reasoning_content", None) or ""
        if delta_reasoning:
            full_reasoning += delta_reasoning

        # 文本 delta
        delta_content = delta.content or ""
        if delta_content:
            if not msg_item_announced:
                msg_item_announced = True
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": msg_item_id, "object": "realtime.item",
                             "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []},
                })
                yield _sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": msg_item_id,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                })
            full_text += delta_content
            yield _sse("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": msg_item_id,
                "output_index": 0, "content_index": 0,
                "delta": delta_content,
            })

        # tool_calls delta
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_buf:
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
                if tc.id:
                    buf["id"] = tc.id
                if tc.function and tc.function.name:
                    buf["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    buf["arguments"] += tc.function.arguments

                if buf["name"] and not buf["announced"]:
                    buf["announced"] = True
                    yield _sse("response.output_item.added", {
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

                if tc.function and tc.function.arguments:
                    yield _sse("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": buf["item_id"],
                        "output_index": buf["output_index"],
                        "delta": tc.function.arguments,
                    })

        if choice.finish_reason and getattr(chunk, "usage", None):
            usage_data = chunk.usage

    # ── 收尾事件 ──
    if msg_item_announced:
        yield _sse("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_item_id,
            "output_index": 0, "content_index": 0,
            "text": full_text,
        })
        yield _sse("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_item_id,
            "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": full_text},
        })
        done_blocks: list[dict] = []
        if full_reasoning:
            done_blocks.append({
                "type": "reasoning", "text": full_reasoning,
                "summary": [{"type": "summary_text", "text": full_reasoning}],
            })
        done_blocks.append({"type": "output_text", "text": full_text, "annotations": []})
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": msg_item_id, "object": "realtime.item",
                "type": "message", "status": "completed",
                "role": "assistant",
                "content": done_blocks,
            },
        })

    final_output: list[dict] = []
    if msg_item_announced:
        final_blocks: list[dict] = []
        if full_reasoning:
            final_blocks.append({
                "type": "reasoning", "text": full_reasoning,
                "summary": [{"type": "summary_text", "text": full_reasoning}],
            })
        final_blocks.append({"type": "output_text", "text": full_text, "annotations": []})
        final_output.append({
            "type": "message", "id": msg_item_id,
            "status": "completed", "role": "assistant",
            "content": final_blocks,
        })

    for idx in sorted(tool_calls_buf):
        buf = tool_calls_buf[idx]
        yield _sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "item_id": buf["item_id"],
            "output_index": buf["output_index"],
            "arguments": buf["arguments"],
        })
        yield _sse("response.output_item.done", {
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

    yield _sse("response.completed", {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "model": model,
            "output": final_output,
            "usage": {
                "input_tokens":  usage_data.prompt_tokens if usage_data else 0,
                "output_tokens": usage_data.completion_tokens if usage_data else 0,
                "total_tokens":  usage_data.total_tokens if usage_data else 0,
            },
        },
    })

    logger.debug(
        "流式完成 response_id=%s text_len=%d tool_calls=%d in=%d out=%d",
        response_id, len(full_text), len(tool_calls_buf),
        usage_data.prompt_tokens if usage_data else 0,
        usage_data.completion_tokens if usage_data else 0,
    )
    _details = getattr(usage_data, "prompt_tokens_details", None) if usage_data else None
    _cache_hit  = getattr(_details, "cached_tokens", 0) or 0
    _cache_miss = max(0, (usage_data.prompt_tokens if usage_data else 0) - _cache_hit)
    record_tokens(
        model,
        usage_data.prompt_tokens if usage_data else 0,
        usage_data.completion_tokens if usage_data else 0,
        usage_data.total_tokens if usage_data else 0,
        input_cache_hit_tokens=_cache_hit,
        input_cache_miss_tokens=_cache_miss,
    )

    yield "data: [DONE]\n\n"
