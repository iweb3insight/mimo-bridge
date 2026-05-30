# mimo-bridge

> Connect [OpenAI Codex CLI](https://github.com/openai/codex) to MiMo Chat Completions API
>
> 将 [OpenAI Codex CLI](https://github.com/openai/codex) 连接到 MiMo Chat Completions API

**Ready to use — function calling works out of the box. For those who want a working setup without the hassle.**

**开箱即用 — function calling 直接可用。适合不想折腾的人。**

---

A lightweight proxy that bridges the **Responses API** (used by Codex CLI) to the **Chat Completions API** (supported by MiMo and compatible models). Handles format conversion, tool call translation, streaming, and conversation history optimization — all in a single Python file.

轻量代理，将 Codex CLI 使用的 **Responses API** 桥接到 MiMo 支持的 **Chat Completions API**。格式转换、工具调用翻译、流式传输、对话历史优化 — 全部在一个 Python 文件里完成。

---

## Features / 功能特性

| Feature | Description |
|---------|-------------|
| **Responses → Chat Completions** | Full format conversion including messages, tools, and streaming / 完整格式转换，包括消息、工具和流式传输 |
| **Tool call translation** | Namespace filtering, flat-to-nested format, `tool_choice` conversion / 命名空间过滤、扁平→嵌套格式、`tool_choice` 转换 |
| **Streaming support** | Real-time SSE event translation with proper `function_call_arguments.done` / 实时 SSE 事件翻译，正确发送 `function_call_arguments.done` |
| **Conversation trimming** | Auto message count limiting, tool output truncation, total char budget / 自动限制消息数、截断工具输出、总字符预算 |
| **Anti-hallucination** | Explicit truncation boundaries prevent model from inferring missing content / 显式截断标记，防止模型推断缺失内容 |
| **Connection pooling** | Persistent `httpx.AsyncClient` with lifecycle management / 持久化连接池，生命周期管理 |
| **Minimal dependencies** | Only FastAPI + uvicorn + httpx / 仅需三个依赖 |
| **Single file** | ~450 lines, easy to audit and customize / ~450 行，易于审计和定制 |

---

## Quick Start / 快速开始

### 1. Clone / 克隆

```bash
git clone https://github.com/iweb3insight/mimo-bridge.git
cd mimo-bridge
```

### 2. Configure / 配置环境变量

```bash
cp .env.example .env
# Edit .env with your MiMo API credentials
# 编辑 .env 填入 MiMo API 凭据
```

### 3. Run / 启动

```bash
# Using uv (recommended — auto-installs dependencies)
# 使用 uv（推荐 — 自动安装依赖）
MIMO_API_KEY="your-key" MIMO_BASE_URL="https://your-endpoint/v1" uv run mimo_bridge.py

# Or using pip / 或使用 pip
pip install fastapi uvicorn httpx
MIMO_API_KEY="your-key" MIMO_BASE_URL="https://your-endpoint/v1" python mimo_bridge.py
```

### 4. Configure Codex CLI / 配置 Codex CLI

Copy `config.example.toml` to `~/.codex/config.toml` and update the paths:

复制 `config.example.toml` 到 `~/.codex/config.toml` 并更新路径：

```toml
model_provider = "mimo"
model = "mimo-v2.5-pro"
model_reasoning_effort = "medium"
model_catalog_json = "/path/to/mimo-bridge/model_catalog.json"

[model_providers.mimo]
name = "mimo"
wire_api = "responses"
base_url = "http://localhost:4000/v1"
experimental_bearer_token = "any-placeholder-token"
```

### 5. Use / 使用

```bash
# Health check / 健康检查
curl http://localhost:4000/health

# Model list / 模型列表
curl http://localhost:4000/v1/models

# Start coding with Codex / 开始用 Codex 编码
codex "create a hello world Python script"
```

---

## How It Works / 工作原理

```
┌─────────────┐        ┌──────────────┐        ┌─────────────────┐
│  Codex CLI  │ ────── │  mimo-bridge │ ────── │  MiMo API       │
│             │        │  (port 4000) │        │  (Chat Completions)│
└─────────────┘        └──────────────┘        └─────────────────┘
     │                       │                         │
     │ Responses API         │ HTTP                    │ Chat Completions
     │ (SSE streaming)       │ (httpx pool)            │ (SSE streaming)
     ▼                       ▼                         ▼
```

### Format Conversion / 格式转换

| Codex (Responses API) | MiMo (Chat Completions) | Notes / 说明 |
|----------------------|------------------------|-------------|
| `instructions` | `system` message | First message / 首条消息 |
| `input[].type=message` | `user`/`assistant` role | Content array → plain text / 内容数组→纯文本 |
| `input[].type=function_call` | `assistant` + `tool_calls` | Merged into single message / 合并为单条消息 |
| `input[].type=function_call_output` | `tool` role + `tool_call_id` | Direct mapping / 直接映射 |
| `tools[].type=namespace` | Filtered out | Codex-specific / Codex 专用，过滤掉 |
| `tools[].type=function` (flat) | `function` (nested) | `name` → `function.name` |
| `tool_choice: {type: "auto"}` | `"auto"` | Simplified / 简化格式 |
| `tool_choice: {type: "function", name: "x"}` | `{type: "function", function: {name: "x"}}` | Nested format / 嵌套格式 |
| `reasoning_effort` | `reasoning_effort` | Forwarded as-is / 原样转发 |

### Streaming Event Sequence / 流式事件序列

```
response.created
response.output_item.added        (message or function_call)
response.content_part.added       (text only)
response.output_text.delta        (text chunks)
response.function_call_arguments.delta  (tool call args)
response.function_call_arguments.done   (tool call complete)
response.content_part.done        (text only)
response.output_item.done
response.completed
```

---

## Configuration / 配置说明

### Environment Variables / 环境变量

| Variable | Default / 默认值 | Description / 说明 |
|----------|-----------------|-------------------|
| `MIMO_BASE_URL` | `https://your-mimo-api-endpoint/v1` | MiMo API base URL |
| `MIMO_API_KEY` | *(empty)* | MiMo API key |

### Proxy Constants / 代理常量

Edit the top of `mimo_bridge.py` / 编辑 `mimo_bridge.py` 顶部：

```python
MAX_MESSAGES = 30           # Keep last N messages / 保留最近 N 条消息
MAX_TOOL_OUTPUT_CHARS = 2000  # Truncate tool outputs / 截断工具输出
MAX_TOTAL_CHARS = 60000     # Total char budget (~20K tokens) / 总字符预算
```

### reasoning_effort

MiMo supports three reasoning levels / MiMo 支持三种推理级别：

| Level | Description / 说明 | Latency / 延迟 |
|-------|-------------------|---------------|
| `low` | Light reasoning / 轻量推理 | ~5-9s (unstable, may return empty / 不稳定，可能返回空) |
| `medium` | Balanced (recommended) / 均衡（推荐） | ~5-7s |
| `high` | Deep reasoning / 深度推理 | ~5-7s |

Configure in `~/.codex/config.toml` / 在 `~/.codex/config.toml` 中配置：

```toml
model_reasoning_effort = "medium"
```

---

## Conversation Trimming / 对话裁剪

Codex CLI sends the **full conversation history** with every request. Without trimming, token counts grow unboundedly.

Codex CLI 每次请求都发送**完整对话历史**。不裁剪的话 token 数会无限增长。

### How it works / 工作原理

1. **Tool output truncation** — tool results capped at `MAX_TOOL_OUTPUT_CHARS` / 工具输出截断 — 单个工具结果限制为 `MAX_TOOL_OUTPUT_CHARS`
2. **Message count limit** — keep system + last `MAX_MESSAGES` / 消息数量限制 — 保留 system + 最近 `MAX_MESSAGES` 条
3. **Total char budget** — drop oldest until under `MAX_TOTAL_CHARS` / 总字符预算 — 丢弃最旧消息直到低于 `MAX_TOTAL_CHARS`

### Anti-hallucination markers / 反幻觉标记

Truncated content includes explicit markers / 截断内容包含显式标记：

```
[⚠️ PROXY TRUNCATED: original 15000 chars, showing first 2000.
 The rest was cut off — do NOT infer or guess the missing content.]

[⚠️ 代理截断：原始 15000 字符，仅显示前 2000。
 剩余内容已截断 — 不要推断或猜测缺失内容。]
```

```
[⚠️ PROXY CONTEXT WINDOW: 70 earlier messages were dropped.
 You have NO knowledge of what was in those messages.
 Do NOT assume or guess — if unsure, ask the user.]

[⚠️ 代理上下文窗口：已丢弃 70 条更早的消息。
 你不知道那些消息的内容。
 不要假设或猜测 — 如果不确定，请询问用户。]
```

---

## Known Issues / 已知问题

### MiMo Singapore API Latency / MiMo 新加坡 API 延迟

The MiMo API endpoint is hosted in Singapore. For users outside Southeast Asia, network latency is significant.

MiMo API 端点托管在新加坡。对于东南亚以外的用户，网络延迟较大。

**Network latency / 网络延迟：**

```
TCP connect:       156ms
TLS handshake:     248ms
Server processing:  94ms
─────────────────────────
Total network:     498ms (one-way / 单程)
```

**Real inference times / 实际推理时间：**

```
  Time          Model            Duration
  ━━━━━━━━━━━━  ━━━━━━━━━━━━━━━  ━━━━━━━━
  21:38:25      mimo-v2.5-pro    8.3s
  ────────────  ───────────────  ────────
  21:38:31      mimo-v2.5-pro    10.1s
  ────────────  ───────────────  ────────
  21:38:41      mimo-v2.5-pro    26.5s
  ────────────  ───────────────  ────────
  21:39:18      mimo-v2.5-pro    29.6s
```

> Context per request / 每次请求上下文: 32 messages, 13 tools, 22K–25K chars (trimmed from 39–47 messages / 从 39-47 条消息裁剪而来)

MiMo is a reasoning model — even simple queries take ~5–8s. Complex multi-tool tasks can take 20–30s. **This is the model's design, not a proxy issue.**

MiMo 是推理模型 — 即使简单查询也需要 ~5-8 秒。复杂的多工具任务可能需要 20-30 秒。**这是模型的设计特性，不是代理的问题。**

**Mitigation / 缓解措施**: Use `reasoning_effort = "medium"` (default). Avoid `"high"` unless you need deep reasoning.

使用 `reasoning_effort = "medium"`（默认）。除非需要深度推理，否则避免使用 `"high"`。

---

## Troubleshooting / 故障排查

### Proxy won't start / 代理无法启动

```bash
# Check port conflict / 检查端口冲突
lsof -ti:4000

# Kill conflicting process / 终止冲突进程
kill $(lsof -ti:4000)
```

### Slow responses (>10s) / 响应慢（>10秒）

MiMo is a reasoning model — even simple requests take ~5s. This is the model's baseline latency, not the proxy.

MiMo 是推理模型 — 即使简单请求也需要约 5 秒。这是模型的基准延迟，不是代理问题。

### MiMo returns empty content / MiMo 返回空内容

MiMo with `reasoning_effort=low` sometimes returns only `reasoning_content` without `content`. Use `medium` or `high`.

MiMo 使用 `reasoning_effort=low` 时有时只返回 `reasoning_content` 而没有 `content`。请使用 `medium` 或 `high`。

### Tool call fails / 工具调用失败

Check proxy logs for format conversion / 检查代理日志中的格式转换：

```
[mimo-bridge] 32 msgs, 13 tools → upstream (stream=True)
```

---

## Development / 开发

```bash
# Install dev dependencies / 安装开发依赖
pip install fastapi uvicorn httpx ruff

# Lint / 代码检查
ruff check mimo_bridge.py

# Run with auto-reload / 热重载运行
uvicorn mimo_bridge:app --host 0.0.0.0 --port 4000 --reload
```

---

## License / 许可证

[MIT](LICENSE)
