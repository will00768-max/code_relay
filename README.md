# Codex → DeepSeek 代理服务

将 OpenAI **`/v1/responses`** API 请求代理转发到 DeepSeek **`/chat/completions`** API。  
使用 **FastAPI** 构建，通过 **OpenAI Python SDK** 调用 DeepSeek。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制示例文件并填入你的 DeepSeek API Key：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_MODEL=deepseek-chat   # 或 deepseek-v4-pro / deepseek-v4-flash
PORT=8000
PROXY_API_KEY=                 # 留空则不校验客户端 key
```

### 3. 启动服务

```bash
python main.py
```

或使用 uvicorn：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

服务启动后访问 `http://localhost:8000/docs` 查看 Swagger 文档。

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | 健康检查 |
| POST | `/v1/responses` | **主代理端点**：Codex Responses API → DeepSeek |
| POST | `/v1/chat/completions` | 透传端点：标准 OpenAI 格式 → DeepSeek |

---

## 请求示例

### 非流式请求（Python）

```python
from openai import OpenAI

client = OpenAI(
    api_key="any-key",           # 若未设置 PROXY_API_KEY，填任意值
    base_url="http://localhost:8000",
)

response = client.responses.create(
    model="deepseek-chat",
    input="用 Python 写一个快速排序",
)
print(response.output[0].content[0].text)
```

### 流式请求（Python）

```python
from openai import OpenAI

client = OpenAI(
    api_key="any-key",
    base_url="http://localhost:8000",
)

with client.responses.stream(
    model="deepseek-chat",
    input="介绍一下 DeepSeek",
) as stream:
    for event in stream:
        if hasattr(event, "delta"):
            print(event.delta, end="", flush=True)
```

### 带 system prompt 的请求

```python
response = client.responses.create(
    model="deepseek-chat",
    instructions="你是一个专业的 Python 开发者，用中文回答。",
    input="什么是装饰器？",
)
```

### cURL 示例

```bash
# 非流式
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "deepseek-chat",
    "input": "Hello, who are you?"
  }'

# 流式
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "input": "写一首关于春天的诗",
    "stream": true
  }'
```

---

## 请求格式转换说明

| Codex `/v1/responses` 字段 | 转换到 DeepSeek 字段 |
|---------------------------|---------------------|
| `input` (string) | `messages[{role:user}]` |
| `input` (list of messages) | `messages` |
| `instructions` / `system` | `messages[0]{role:system}` |
| `model` | `model`（未指定则用 `DEEPSEEK_MODEL`） |
| `max_output_tokens` | `max_tokens` |
| `temperature` | `temperature` |
| `top_p` | `top_p` |
| `stream` | `stream` |
| `tools` | `tools` |
| `tool_choice` | `tool_choice` |
| `response_format` | `response_format` |

---

## 响应格式

### 非流式响应

```json
{
  "id": "resp_xxxxxxxxxxxx",
  "object": "response",
  "created_at": 1714000000,
  "status": "completed",
  "model": "deepseek-chat",
  "output": [
    {
      "type": "message",
      "id": "msg_xxxxxxxxxxxx",
      "status": "completed",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "模型回复内容...",
          "annotations": []
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 10,
    "output_tokens": 50,
    "total_tokens": 60
  }
}
```

### 流式响应（SSE 事件序列）

```
event: response.created
data: {...}

event: response.in_progress
data: {...}

event: response.output_item.added
data: {...}

event: response.content_part.added
data: {...}

event: response.output_text.delta   ← 增量文本（多次）
data: {"delta": "Hello"}

event: response.output_text.done
data: {...}

event: response.content_part.done
data: {...}

event: response.output_item.done
data: {...}

event: response.completed
data: {...}

data: [DONE]
```

---

## 项目结构

```
code_relay/
├── main.py           # FastAPI 应用主文件
├── requirements.txt  # Python 依赖
├── .env.example      # 环境变量示例
├── .env              # 实际环境变量（请勿提交到 git）
└── README.md         # 本文档
```
