# mimo-bridge

> Connect [OpenAI Codex CLI](https://github.com/openai/codex) to MiMo Chat Completions API

A lightweight proxy that bridges the **Responses API** (used by Codex CLI) to the **Chat Completions API** (supported by MiMo and compatible models). Handles format conversion, tool call translation, streaming, and conversation history optimization — all in a single Python file.

---

## Features

- **Responses API → Chat Completions** — full format conversion including messages, tools, and streaming
- **Tool call translation** — namespace filtering, flat-to-nested format, `tool_choice` conversion
- **Streaming support** — real-time SSE event translation with proper `function_call_arguments.done` events
- **Conversation trimming** — automatic message count limiting, tool output truncation, total char budget
- **Anti-hallucination markers** — explicit truncation boundaries prevent model from inferring missing content
- **Connection pooling** — persistent `httpx.AsyncClient` with lifecycle management
- **Zero dependencies beyond the standard trio** — FastAPI + uvicorn + httpx
- **Single file** — ~450 lines, easy to audit and customize

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/mimo-bridge.git
cd mimo-bridge
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your MiMo API credentials
```

### 3. Run

```bash
# Using uv (recommended — auto-installs dependencies)
MIMO_API_KEY="your-key" MIMO_BASE_URL="https://your-endpoint/v1" uv run mimo_bridge.py

# Or using pip
pip install fastapi uvicorn httpx
MIMO_API_KEY="your-key" MIMO_BASE_URL="https://your-endpoint/v1" python mimo_bridge.py
```

### 4. Configure Codex CLI

Copy `config.example.toml` to `~/.codex/config.toml` and update the paths:

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

### 5. Use

```bash
# Health check
curl http://localhost:4000/health

# Model list
curl http://localhost:4000/v1/models

# Start coding with Codex
codex "create a hello world Python script"
```

---

## How It Works

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

### Format Conversion

| Codex (Responses API) | MiMo (Chat Completions) | Notes |
|----------------------|------------------------|-------|
| `instructions` | `system` message | First message |
| `input[].type=message` | `user`/`assistant` role | Content array → plain text |
| `input[].type=function_call` | `assistant` + `tool_calls` | Merged into single message |
| `input[].type=function_call_output` | `tool` role + `tool_call_id` | Direct mapping |
| `tools[].type=namespace` | Filtered out | Codex-specific |
| `tools[].type=function` (flat) | `function` (nested) | `name` → `function.name` |
| `tool_choice: {type: "auto"}` | `"auto"` | Simplified |
| `tool_choice: {type: "function", name: "x"}` | `{type: "function", function: {name: "x"}}` | Nested format |
| `reasoning_effort` | `reasoning_effort` | Forwarded as-is |

### Streaming Event Sequence

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

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMO_BASE_URL` | `https://your-mimo-api-endpoint/v1` | MiMo API base URL |
| `MIMO_API_KEY` | *(empty)* | MiMo API key |

### Proxy Constants

Edit the top of `mimo_bridge.py`:

```python
MAX_MESSAGES = 30           # Keep last N conversation messages
MAX_TOOL_OUTPUT_CHARS = 2000  # Truncate tool outputs beyond this
MAX_TOTAL_CHARS = 60000     # Total char budget (~20K tokens)
```

### reasoning_effort

MiMo supports three reasoning levels:

| Level | Description | Latency |
|-------|-------------|---------|
| `low` | Light reasoning | ~5-9s (unstable, may return empty) |
| `medium` | Balanced (recommended) | ~5-7s |
| `high` | Deep reasoning | ~5-7s |

Configure in `~/.codex/config.toml`:

```toml
model_reasoning_effort = "medium"
```

---

## Conversation Trimming

Codex CLI sends the **full conversation history** with every request. Without trimming, token counts grow unboundedly.

### How it works

1. **Tool output truncation** — individual tool results capped at `MAX_TOOL_OUTPUT_CHARS`
2. **Message count limit** — keep system messages + last `MAX_MESSAGES` conversation messages
3. **Total char budget** — drop oldest messages until under `MAX_TOTAL_CHARS`

### Anti-hallucination markers

Truncated content includes explicit markers telling the model **not to infer missing data**:

```
[⚠️ PROXY TRUNCATED: original 15000 chars, showing first 2000.
 The rest was cut off — do NOT infer or guess the missing content.]
```

```
[⚠️ PROXY CONTEXT WINDOW: 70 earlier messages were dropped.
 You have NO knowledge of what was in those messages.
 Do NOT assume or guess — if unsure, ask the user.]
```

---

## Troubleshooting

### Proxy won't start

```bash
# Check port conflict
lsof -ti:4000

# Kill conflicting process
kill $(lsof -ti:4000)
```

### Slow responses (>10s)

MiMo is a reasoning model — even simple requests take ~5s. This is the model's baseline latency, not the proxy.

### MiMo returns empty content

MiMo with `reasoning_effort=low` sometimes returns only `reasoning_content` without `content`. Use `medium` or `high`.

### Tool call fails

Check proxy logs for format conversion:

```
[mimo-bridge] 32 msgs, 13 tools → upstream (stream=True)
```

---

## Development

```bash
# Install dev dependencies
pip install fastapi uvicorn httpx ruff

# Lint
ruff check mimo_bridge.py

# Run with auto-reload
uvicorn mimo_bridge:app --host 0.0.0.0 --port 4000 --reload
```

---

## License

[MIT](LICENSE)
