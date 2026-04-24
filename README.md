# Codex → DeepSeek 代理服务

将 OpenAI **Responses API**（`/v1/responses`）请求代理转发到 DeepSeek **Chat Completions API**（`/chat/completions`），同时完整保留流式 SSE 事件格式、工具调用、思维链（reasoning）等特性。使用 **FastAPI** 构建，支持管理面板、Token 统计、余额监控。

> **GitHub**：[https://github.com/will00768-max/code_relay](https://github.com/will00768-max/code_relay)

---

## 目录

- [功能特性](#功能特性)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [环境变量说明](#环境变量说明)
- [Docker 部署](#docker-部署)
- [Codex CLI 配置教程](#codex-cli-配置教程)
- [API 端点](#api-端点)
- [请求格式转换说明](#请求格式转换说明)
- [管理面板](#管理面板)
- [模型名映射](#模型名映射)
- [数据存储](#数据存储)

---

## 功能特性

- **完整格式转换**：Responses API input/output ↔ Chat Completions messages，支持字符串、消息数组、多轮对话
- **流式 SSE 事件**：完整实现 `response.created` → `response.output_text.delta` → `response.completed` 事件序列
- **工具调用（Tool Calls）**：支持非流式和流式两种场景下的 function_call / function_call_output
- **思维链（Reasoning）**：透传 DeepSeek 的 `reasoning_content`，保留推理过程
- **多模态图片透传**：支持 `image_url`、Codex `image/url`、`image/base64` 三种图片格式（需模型支持）
- **模型名映射**：自动将 `deepseek-chat`、`deepseek-v3` 等别名映射到 DeepSeek 实际支持的模型
- **Chat Completions 透传**：`/v1/chat/completions` 端点直接转发，无需格式转换
- **可选鉴权**：配置 `PROXY_API_KEY` 后对所有代理请求进行 Bearer Token 校验
- **Token 统计**：每次调用后自动记录到 SQLite，按模型、日期分组，含缓存命中/未命中统计
- **余额监控**：定时查询 DeepSeek 账户余额，追踪今日/累计消费
- **管理面板**：可视化看板，趋势图、调用记录、模型配置，实时刷新

---

## 项目结构

```
code_relay/
├── main.py                  # FastAPI 应用入口，路由注册，uvicorn 启动
├── requirements.txt         # Python 依赖
├── Dockerfile               # Docker 镜像构建
├── docker-compose.yml       # Docker Compose 一键启动
├── .dockerignore
├── .env                     # 环境变量（不提交到 git）
├── .env.example             # 环境变量示例
├── README.md
└── app/
    ├── __init__.py          # 包初始化：触发日志配置 + 数据库建表
    ├── config.py            # 环境变量加载，模型名映射表
    ├── converters.py        # 核心：Responses API ↔ Chat Completions 格式转换
    ├── database.py          # SQLite 读写：token_stats + balance_snapshot
    ├── logger.py            # 全局日志配置（文件 + 控制台）
    ├── data/                # 运行时数据（自动创建）
    │   ├── stats.db         # SQLite 数据库
    │   └── relay.log        # 运行日志
    ├── routes/
    │   ├── proxy.py         # 代理路由：/v1/responses、/v1/chat/completions
    │   └── admin.py         # 管理路由：面板、余额、统计、模型配置
    └── static/
        ├── index.html       # 管理面板前端（Vue 3 + Chart.js，CDN 加载）
        ├── style.css        # 管理面板样式（深色主题）
        ├── app.js           # 管理面板逻辑
        └── readme.html      # 文档渲染页（marked.js 渲染 README.md）
```

---

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/will00768-max/code_relay.git
cd code_relay
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

依赖清单：

| 包 | 说明 |
|----|------|
| `fastapi` | Web 框架 |
| `uvicorn[standard]` | ASGI 服务器（含 WebSocket、热更新支持） |
| `openai` | DeepSeek API 调用（OpenAI 兼容 SDK） |
| `python-dotenv` | 从 `.env` 文件加载环境变量 |
| `httpx` | 异步 HTTP 客户端（用于余额查询等） |
| `aiofiles` | 异步文件读写 |

### 3. 配置环境变量

复制示例文件：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写 `DEEPSEEK_API_KEY`：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_MODEL=deepseek-v4-flash
PORT=8000
PROXY_API_KEY=
```

### 4. 启动服务

```bash
python main.py
```

或使用 uvicorn：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

启动后访问：
- **管理面板**：`http://localhost:8000/admin`
- **使用文档**：`http://localhost:8000/readme`
- **Swagger API 文档**：`http://localhost:8000/docs`
- **健康检查**：`http://localhost:8000/`

---

## 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | （必填）| DeepSeek 平台 API Key，前往 [platform.deepseek.com](https://platform.deepseek.com) 获取 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 基础地址，一般无需修改 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 默认模型，当请求未指定 `model` 时使用；也可在管理面板中动态修改 |
| `PROXY_API_KEY` | （空，不校验）| 代理服务自身的鉴权 Key。设置后客户端必须在请求头携带 `Authorization: Bearer <key>` |
| `PORT` | `8000` | 服务监听端口 |

> `PROXY_API_KEY` 留空则所有客户端均可访问，适合本机使用；若暴露到公网建议设置。

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

> 数据库和日志会持久化到本地 `./data/` 目录（映射到容器内 `/app/app/data`），容器重建后历史数据不会丢失。

---

### 方式二：纯 Docker 命令

**构建镜像**

```bash
docker build -t codex-relay .
```

**运行容器（Linux / macOS）**

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

**运行容器（Windows PowerShell）**

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

> 适用于 **Codex CLI**（命令行工具）、**VSCode Codex 插件** 和 **Codex**（[codex.openai.com](https://codex.openai.com) Web 端），配置完成后重启即可生效。

### 配置文件路径

| 系统 | 路径 |
|------|------|
| Windows | `C:\Users\<用户名>\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

### 完整配置内容

```toml
# 指定使用自定义 DeepSeek provider
model_provider = "deepseek"

# 模型名，代理服务会将其转发到 DeepSeek 实际模型
model = "deepseek-v4-flash"

# 推理努力程度：low / medium / high
model_reasoning_effort = "high"

# 允许联网访问
network_access = "enabled"

# 禁用云端对话存储（推荐开启，保护隐私）
disable_response_storage = true

# Windows 用户需要此项（Linux/macOS 可删除）
windows_wsl_setup_acknowledged = true

# 日志详细程度：low / medium / high
model_verbosity = "high"

# 自定义 provider 配置
[model_providers.deepseek]
name = "DeepSeek"

# 指向本代理服务地址
base_url = "http://127.0.0.1:8000"

# 使用 OpenAI Responses API 协议（本代理的核心协议）
wire_api = "responses"

# 携带 Bearer Token（Codex 会使用本地已登录的 OpenAI key，代理服务不校验）
requires_openai_auth = true
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `model_provider` | 必须设为 `"deepseek"`，匹配下方 `[model_providers.deepseek]` 的 key |
| `model` | 模型名，代理会自动将 `deepseek-chat`、`deepseek-v3` 等别名映射到正确模型 |
| `model_reasoning_effort` | 控制 Codex 发送请求时的推理预算（影响 `max_tokens` 等参数） |
| `base_url` | 代理服务地址，默认 `http://127.0.0.1:8000`；若部署到远程服务器需改为对应 IP |
| `wire_api` | 必须设为 `"responses"`，Codex 将使用 `/v1/responses` 端点发送请求 |
| `requires_openai_auth` | 设为 `true` 后 Codex 会在请求头附加 `Authorization: Bearer <token>`；代理服务未设置 `PROXY_API_KEY` 时不校验此 token |
| `disable_response_storage` | 禁用 OpenAI 云端对话存储，对话内容不会上传 |

### 生效方式

配置保存后：
- **Codex CLI**：重新打开终端，或执行 `codex` 命令时自动读取最新配置
- **VSCode Codex 插件**：重启 VSCode，或按 `Ctrl+Shift+P` 输入 `Reload Window`

---

## API 端点

### 代理端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/responses` | **主代理端点**：接收 Codex Responses API 格式，转换后发给 DeepSeek，响应转回 Responses API 格式 |
| `POST` | `/responses` | 同上（不带 `/v1` 前缀，兼容部分客户端） |
| `POST` | `/v1/chat/completions` | **透传端点**：标准 OpenAI Chat Completions 格式，直接转发到 DeepSeek，无需格式转换 |
| `POST` | `/chat/completions` | 同上（不带 `/v1` 前缀） |

### 管理端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 健康检查，返回 `{"status": "ok"}` |
| `GET` | `/admin` | 管理面板 HTML 页面 |
| `GET` | `/readme` | 文档页面（Markdown 渲染） |
| `GET` | `/admin/balance` | 查询 DeepSeek 账户余额及今日/累计消费 |
| `GET` | `/admin/stats` | Token 统计（今日/总计，按模型分组，含费用估算） |
| `GET` | `/admin/calls` | 调用详情（按模型分组 + 最近 200 条记录） |
| `GET` | `/admin/chart?days=30` | 图表数据（最近 N 天，范围 7~90 天） |
| `GET` | `/admin/models` | 从 DeepSeek API 获取可用模型列表（失败时返回内置备用列表） |
| `GET` | `/admin/model` | 获取当前默认模型 |
| `POST` | `/admin/model` | 动态修改默认模型（`{"model": "deepseek-v4-pro"}`，服务内存生效，重启后恢复 `.env` 配置） |

---

## 请求格式转换说明

### `/v1/responses` 请求字段映射

| Codex Responses API 字段 | 转换到 DeepSeek Chat Completions 字段 | 说明 |
|--------------------------|--------------------------------------|------|
| `input`（字符串） | `messages: [{role: "user", content: "..."}]` | 直接包装为 user 消息 |
| `input`（消息数组） | `messages` | 完整多轮对话转换 |
| `input[].type = "message"` | 对应 role 的 message | role 支持 `user`/`system`/`assistant`/`tool` |
| `input[].type = "function_call"` | `assistant.tool_calls[]` | 工具调用请求，合并到 assistant 消息 |
| `input[].type = "function_call_output"` | `{role: "tool", tool_call_id, content}` | 工具调用结果 |
| `input[].role = "developer"` | `role: "system"` | 角色名映射 |
| `instructions` / `system` | `messages[0]: {role: "system"}` | 插入到消息列表头部 |
| `max_output_tokens` | `max_tokens` | 最大输出 token 数 |
| `temperature` | `temperature` | 温度参数，直接透传 |
| `top_p` | `top_p` | 直接透传 |
| `tools` | `tools`（标准化为 function 格式） | 工具定义，支持扁平格式和嵌套格式 |
| `tool_choice` | `tool_choice` | 直接透传 |
| `response_format` | `response_format` | 直接透传 |
| `text.format.type = "json_object"` | `response_format: {type: "json_object"}` | JSON 模式 |
| `stream` | `stream` | 流式开关 |

### 图片内容格式支持

user/system 消息中的图片块会自动转换为 DeepSeek 支持的 `image_url` 格式：

| 输入格式 | 转换为 |
|----------|--------|
| `{type: "image_url", image_url: {url: "..."}}` | 直接透传（OpenAI 标准格式） |
| `{type: "image", source: {type: "url", url: "..."}}` | `{type: "image_url", image_url: {url: "..."}}` |
| `{type: "image", source: {type: "base64", data: "...", media_type: "image/png"}}` | `{type: "image_url", image_url: {url: "data:image/png;base64,..."}}` |

> **注意**：DeepSeek 当前文本模型（`deepseek-v4-flash`、`deepseek-v4-pro`）不支持图片输入，发送图片请求会返回 API 错误。

### `/v1/responses` 响应格式

**非流式响应**

```json
{
  "id": "resp_xxxxxxxxxxxxxxxxxxxx",
  "object": "response",
  "created_at": 1714000000,
  "status": "completed",
  "model": "deepseek-v4-flash",
  "output": [
    {
      "type": "message",
      "id": "msg_xxxxxxxxxxxxxxxxxxxx",
      "status": "completed",
      "role": "assistant",
      "content": [
        {
          "type": "reasoning",
          "text": "（思维链内容，仅 thinking 模式下存在）",
          "summary": [{"type": "summary_text", "text": "..."}]
        },
        {
          "type": "output_text",
          "text": "模型回复内容",
          "annotations": []
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 10,
    "input_cache_hit_tokens": 5,
    "input_cache_miss_tokens": 5,
    "output_tokens": 50,
    "total_tokens": 60
  }
}
```

如果响应中包含工具调用，`output` 数组中会额外追加：

```json
{
  "type": "function_call",
  "id": "fc_xxxx",
  "call_id": "call_xxxx",
  "name": "工具函数名",
  "arguments": "{\"key\": \"value\"}",
  "status": "completed"
}
```

**流式 SSE 事件序列**

```
event: response.created
data: {"type": "response.created", "response": {...}}

event: response.in_progress
data: {"type": "response.in_progress", "response": {"id": "..."}}

event: response.output_item.added
data: {"type": "response.output_item.added", "output_index": 0, "item": {...}}

event: response.content_part.added
data: {"type": "response.content_part.added", "item_id": "...", ...}

event: response.output_text.delta        ← 文本增量（多次）
data: {"type": "response.output_text.delta", "delta": "Hello"}

event: response.output_text.done
data: {"type": "response.output_text.done", "text": "完整文本"}

event: response.content_part.done
data: {...}

event: response.output_item.done
data: {...}

event: response.completed
data: {"type": "response.completed", "response": {"status": "completed", ...}}

data: [DONE]
```

如果流式响应包含工具调用，还会穿插：

```
event: response.output_item.added       ← 工具调用项开始
event: response.function_call_arguments.delta  ← 参数增量（多次）
event: response.function_call_arguments.done   ← 参数完成
event: response.output_item.done        ← 工具调用项完成
```

---

## 管理面板

访问 `http://localhost:8000/admin` 打开管理面板，功能包括：

**左侧边栏**

| 卡片 | 说明 |
|------|------|
| 账户余额 | 实时余额、今日消费、累计消费（需配置 `DEEPSEEK_API_KEY`） |
| 模型配置 | 下拉选择或手动输入模型名，动态修改默认模型（服务重启后恢复 `.env` 设置） |
| 今日 Token | 今日输入/输出/总 Token 数及估算费用 |
| 累计 Token | 历史累计 Token 数及估算费用 |

**右侧主区域**

| 区块 | 说明 |
|------|------|
| 趋势图 | 最近 30 天每日调用次数趋势（按模型堆叠）+ Token 用量趋势（缓存命中/未命中/输出） |
| API 调用情况 | 三个 Tab：今日（按模型分组）、累计（按模型分组）、最近记录（最近 200 条，每页 20 条） |

**费用估算价格表**（元/百万 Token）

| 模型 | 输入 | 输出 |
|------|------|------|
| `deepseek-v4-flash` | 1.0 | 2.0 |
| `deepseek-v4-pro` | 12.0 | 24.0 |
| `deepseek-chat` | 1.0 | 2.0 |
| `deepseek-reasoner` | 4.0 | 16.0 |

---

## 模型名映射

代理服务在转发请求前会自动规范化模型名，避免因模型名不一致导致 API 报错：

| 请求中的模型名 | 实际发给 DeepSeek 的模型名 |
|----------------|---------------------------|
| `deepseek-v4-flash` | `deepseek-v4-flash` |
| `deepseek-v4-pro` | `deepseek-v4-pro` |
| `deepseek-chat` | `deepseek-v4-flash` |
| `deepseek-reasoner` | `deepseek-v4-flash` |
| `deepseek-v3` | `deepseek-v4-flash` |
| `deepseek-v4` | `deepseek-v4-flash` |
| `deepseek-v4-5` | `deepseek-v4-flash` |
| 含 `pro` 的名称 | `deepseek-v4-pro` |
| 其他 `deepseek-*` | `deepseek-v4-flash` |
| 其他未知名称 | 使用 `DEEPSEEK_MODEL` 环境变量值 |

---

## 数据存储

运行时数据存储在 `app/data/` 目录（服务启动时自动创建）：

### `app/data/stats.db`（SQLite）

**`token_stats` 表** — 每次 API 调用写入一条记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 自增主键 |
| `ts` | TEXT | 日期（`YYYY-MM-DD`） |
| `ts_full` | TEXT | 时间（`HH:MM:SS`） |
| `model` | TEXT | 模型名 |
| `input_tokens` | INTEGER | 输入 Token 数 |
| `input_cache_hit_tokens` | INTEGER | 缓存命中的输入 Token 数 |
| `input_cache_miss_tokens` | INTEGER | 缓存未命中的输入 Token 数 |
| `output_tokens` | INTEGER | 输出 Token 数 |
| `total_tokens` | INTEGER | 总 Token 数 |

**`balance_snapshot` 表** — 每次查询余额时写入一条快照

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 自增主键 |
| `ts` | TEXT | 完整时间戳 |
| `date` | TEXT | 日期（用于计算今日消费） |
| `balance` | REAL | 余额（元） |

### `app/data/relay.log`

服务运行日志，包含每次请求的模型名、流式/非流式、Token 统计、错误信息等。日志格式：

```
2026-04-24 10:00:00,000 [INFO] 收到请求 model=deepseek-v4-flash stream=True
2026-04-24 10:00:01,500 [DEBUG] 流式完成 response_id=resp_xxx text_len=256 tool_calls=0 in=120 out=80
```

---

## 使用示例

### Python（非流式）

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

### Python（流式）

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

### Python（多轮对话 + 工具调用）

```python
response = client.responses.create(
    model="deepseek-v4-flash",
    input=[
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "今天天气怎么样？"},
    ],
    tools=[{
        "name": "get_weather",
        "description": "查询天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }],
)
```

### cURL（非流式）

```bash
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "deepseek-v4-flash",
    "input": "Hello, who are you?"
  }'
```

### cURL（流式）

```bash
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "input": "写一首关于春天的诗",
    "stream": true
  }'
```

### cURL（Chat Completions 透传）

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```
