# Codex → DeepSeek 代理服务

将 OpenAI **`/v1/responses`** API 请求代理转发到 DeepSeek **`/chat/completions`** API。  
使用 **FastAPI** 构建，通过 **OpenAI Python SDK** 调用 DeepSeek。

> **GitHub**：[https://github.com/liao65656/code_relay](https://github.com/liao65656/code_relay)

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
DEEPSEEK_MODEL=deepseek-v4-flash   # 或 deepseek-v4-pro
PORT=8000
PROXY_API_KEY=                     # 留空则不校验客户端 key
```

### 3. 启动服务

```bash
python main.py
```

或使用 uvicorn：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

服务启动后访问 `http://localhost:8000` 查看管理面板，`http://localhost:8000/docs` 查看 Swagger 文档。

---

## Docker 部署

### 方式一：Docker Compose（推荐）

**1. 准备环境变量文件**

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

**2. 启动服务**

```bash
docker compose up -d
```

**3. 查看日志**

```bash
docker compose logs -f
```

**4. 停止服务**

```bash
docker compose down
```

> 数据库和日志会持久化到本地 `./data/` 目录，容器重建后数据不会丢失。

---

### 方式二：纯 Docker 命令

**构建镜像**

```bash
docker build -t codex-relay .
```

**运行容器**

```bash
docker run -d \
  --name codex-relay \
  --restart unless-stopped \
  -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx \
  -e DEEPSEEK_MODEL=deepseek-v4-flash \
  -v $(pwd)/data:/app/app/data \
  codex-relay
```

Windows PowerShell 下将 `$(pwd)` 替换为 `${PWD}`：

```powershell
docker run -d `
  --name codex-relay `
  --restart unless-stopped `
  -p 8000:8000 `
  -e DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx `
  -e DEEPSEEK_MODEL=deepseek-v4-flash `
  -v ${PWD}/data:/app/app/data `
  codex-relay
```

---

## Codex CLI 配置教程

> 适用于 **Codex CLI**（命令行）和 **VSCode Codex 插件**，配置完成后重启即可生效。

### 配置文件路径

| 系统 | 路径 |
|------|------|
| Windows | `C:\Users\<用户名>\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

### 配置内容

编辑 `config.toml`，添加或修改以下内容：

```toml
# 使用自定义 DeepSeek provider
model_provider = "deepseek"
model = "deepseek-v4-flash"
model_reasoning_effort = "high"
network_access = "enabled"
disable_response_storage = true
windows_wsl_setup_acknowledged = true   # 仅 Windows 需要
model_verbosity = "high"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:8000"      # 本代理服务地址
wire_api = "responses"
requires_openai_auth = true
```

### 关键字段说明

| 字段 | 说明 |
|------|------|
| `model_provider` | 设为 `"deepseek"` 以使用下方自定义 provider |
| `model` | 模型名，代理服务会将其映射到 DeepSeek 实际模型 |
| `base_url` | 本代理服务地址，默认 `http://127.0.0.1:8000` |
| `wire_api` | 必须设为 `"responses"`，使用 OpenAI Responses API 协议 |
| `requires_openai_auth` | 设为 `true`，Codex 会在请求头带上 Bearer Token |
| `disable_response_storage` | 设为 `true`，禁用云端对话存储 |

### 生效方式

配置保存后：
- **Codex CLI**：重新打开终端或重启 `codex` 命令
- **VSCode Codex 插件**：重启 VSCode 或重新加载窗口（`Ctrl+Shift+P` → `Reload Window`）

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | 管理面板 |
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
    model="deepseek-v4-flash",
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
    model="deepseek-v4-flash",
    input="介绍一下 DeepSeek",
) as stream:
    for event in stream:
        if hasattr(event, "delta"):
            print(event.delta, end="", flush=True)
```

### cURL 示例

```bash
# 非流式
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "deepseek-v4-flash",
    "input": "Hello, who are you?"
  }'

# 流式
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
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
  "model": "deepseek-v4-flash",
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
    "input_cache_hit_tokens": 0,
    "input_cache_miss_tokens": 10,
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
├── main.py               # FastAPI 应用入口
├── requirements.txt      # Python 依赖
├── .env.example          # 环境变量示例
├── .env                  # 实际环境变量（请勿提交到 git）
├── README.md             # 本文档
└── app/
    ├── config.py         # 配置加载
    ├── converters.py     # 请求/响应格式转换
    ├── database.py       # SQLite Token 统计
    └── routes/
        ├── proxy.py      # 代理路由
        └── admin.py      # 管理面板 API
```
