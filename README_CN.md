# mimo-bridge

[English](README.md)

> 将 [OpenAI Codex CLI](https://github.com/openai/codex) 连接到 MiMo Chat Completions API

**开箱即用 — function calling 直接可用。适合不想折腾的人。**

轻量代理，将 Codex CLI 使用的 **Responses API** 桥接到 MiMo 支持的 **Chat Completions API**。格式转换、工具调用翻译、流式传输、对话历史优化 — 全部在一个 Python 文件里完成。

---

## 功能特性

- **Responses API → Chat Completions** — 完整格式转换，包括消息、工具和流式传输
- **工具调用翻译** — 命名空间过滤、扁平→嵌套格式、`tool_choice` 转换
- **流式传输** — 实时 SSE 事件翻译，正确发送 `function_call_arguments.done`
- **对话裁剪** — 自动限制消息数、截断工具输出、总字符预算
- **反幻觉标记** — 显式截断边界，防止模型推断缺失内容
- **连接池** — 持久化 `httpx.AsyncClient`，生命周期管理
- **最小依赖** — 仅需 FastAPI + uvicorn + httpx
- **单文件** — ~450 行，易于审计和定制

---

## 快速开始

### 1. 克隆

```bash
git clone https://github.com/iweb3insight/mimo-bridge.git
cd mimo-bridge
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 MiMo API 凭据
```

### 3. 启动

```bash
# 使用 uv（推荐 — 自动安装依赖）
MIMO_API_KEY="your-key" MIMO_BASE_URL="https://your-endpoint/v1" uv run mimo_bridge.py

# 或使用 pip
pip install fastapi uvicorn httpx
MIMO_API_KEY="your-key" MIMO_BASE_URL="https://your-endpoint/v1" python mimo_bridge.py
```

### 4. 配置 Codex CLI

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

### 5. 使用

```bash
# 健康检查
curl http://localhost:4000/health

# 模型列表
curl http://localhost:4000/v1/models

# 开始用 Codex 编码
codex "create a hello world Python script"
```

---

## 工作原理

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

### 格式转换

| Codex (Responses API) | MiMo (Chat Completions) | 说明 |
|----------------------|------------------------|------|
| `instructions` | `system` message | 首条消息 |
| `input[].type=message` | `user`/`assistant` role | 内容数组→纯文本 |
| `input[].type=function_call` | `assistant` + `tool_calls` | 合并为单条消息 |
| `input[].type=function_call_output` | `tool` role + `tool_call_id` | 直接映射 |
| `tools[].type=namespace` | 过滤掉 | Codex 专用 |
| `tools[].type=function` (扁平) | `function` (嵌套) | `name` → `function.name` |
| `tool_choice: {type: "auto"}` | `"auto"` | 简化格式 |
| `tool_choice: {type: "function", name: "x"}` | `{type: "function", function: {name: "x"}}` | 嵌套格式 |
| `reasoning_effort` | `reasoning_effort` | 原样转发 |

### 流式事件序列

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

## 配置说明

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MIMO_BASE_URL` | `https://your-mimo-api-endpoint/v1` | MiMo API 地址 |
| `MIMO_API_KEY` | *(空)* | MiMo API 密钥 |

### 代理常量

编辑 `mimo_bridge.py` 顶部：

```python
MAX_MESSAGES = 30           # 保留最近 N 条消息
MAX_TOOL_OUTPUT_CHARS = 2000  # 截断工具输出
MAX_TOTAL_CHARS = 60000     # 总字符预算 (~20K tokens)
```

### reasoning_effort

MiMo 支持三种推理级别：

| 级别 | 说明 | 延迟 |
|------|------|------|
| `low` | 轻量推理 | ~5-9s（不稳定，可能返回空） |
| `medium` | 均衡（推荐） | ~5-7s |
| `high` | 深度推理 | ~5-7s |

在 `~/.codex/config.toml` 中配置：

```toml
model_reasoning_effort = "medium"
```

---

## 对话裁剪

Codex CLI 每次请求都发送**完整对话历史**。不裁剪的话 token 数会无限增长。

### 工作原理

1. **工具输出截断** — 单个工具结果限制为 `MAX_TOOL_OUTPUT_CHARS`
2. **消息数量限制** — 保留 system + 最近 `MAX_MESSAGES` 条消息
3. **总字符预算** — 丢弃最旧消息直到低于 `MAX_TOTAL_CHARS`

### 反幻觉标记

截断内容包含显式标记，告诉模型**不要推断缺失数据**：

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

## 已知问题

### MiMo 新加坡 API 延迟

MiMo API 端点托管在新加坡。对于东南亚以外的用户，网络延迟较大。

**网络延迟：**

```
TCP 连接:      156ms
TLS 握手:      248ms
服务端处理:     94ms
──────────────────────
总网络延迟:    498ms（单程）
```

**实际推理时间：**

```
  时间          模型              耗时
  ━━━━━━━━━━━━  ━━━━━━━━━━━━━━━  ━━━━━━━━
  21:38:25      mimo-v2.5-pro    8.3s
  ────────────  ───────────────  ────────
  21:38:31      mimo-v2.5-pro    10.1s
  ────────────  ───────────────  ────────
  21:38:41      mimo-v2.5-pro    26.5s
  ────────────  ───────────────  ────────
  21:39:18      mimo-v2.5-pro    29.6s
```

> 每次请求上下文：32 条消息、13 个工具、22K–25K 字符（从 39-47 条消息裁剪而来）

MiMo 是推理模型 — 即使简单查询也需要 ~5-8 秒。复杂的多工具任务可能需要 20-30 秒。**这是模型的设计特性，不是代理的问题。**

**缓解措施**：使用 `reasoning_effort = "medium"`（默认）。除非需要深度推理，否则避免使用 `"high"` — 它不会减少延迟，但可能提升复杂任务的输出质量。

---

## 故障排查

### 代理无法启动

```bash
# 检查端口冲突
lsof -ti:4000

# 终止冲突进程
kill $(lsof -ti:4000)
```

### 响应慢（>10秒）

MiMo 是推理模型 — 即使简单请求也需要约 5 秒。这是模型的基准延迟，不是代理问题。

### MiMo 返回空内容

MiMo 使用 `reasoning_effort=low` 时有时只返回 `reasoning_content` 而没有 `content`。请使用 `medium` 或 `high`。

### 工具调用失败

检查代理日志中的格式转换：

```
[mimo-bridge] 32 msgs, 13 tools → upstream (stream=True)
```

---

## 开发

```bash
# 安装开发依赖
pip install fastapi uvicorn httpx ruff

# 代码检查
ruff check mimo_bridge.py

# 热重载运行
uvicorn mimo_bridge:app --host 0.0.0.0 --port 4000 --reload
```

---

## 许可证

[MIT](LICENSE)
