#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.100.0",
#     "uvicorn>=0.23.0",
#     "httpx>=0.24.0",
# ]
# ///
"""
mimo-bridge: Connect OpenAI Codex CLI to MiMo Chat Completions API.

Bridges the Responses API (used by Codex CLI) to Chat Completions API
(supported by MiMo and other compatible models), with conversation
history trimming, tool format conversion, and streaming support.
"""
import asyncio
import json
import logging
import os
import time
import traceback
import uuid
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("mimo-bridge")

MIMO_BASE = os.environ.get("MIMO_BASE_URL", "https://your-mimo-api-endpoint/v1")
MIMO_KEY = os.environ.get("MIMO_API_KEY", "")

# ── Optimization constants ──
MAX_MESSAGES = 30          # keep system + last N messages
MAX_TOOL_OUTPUT_CHARS = 2000  # truncate tool results beyond this
MAX_TOTAL_CHARS = 60000    # ~20k token budget for prompt

# ── Persistent HTTP client with connection pooling ──
_http_client: httpx.AsyncClient = None  # type: ignore[assignment]


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )
    logger.info("HTTP client initialized")


@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        logger.info("HTTP client closed")


def _estimate_chars(messages):
    """Estimate total character count across all messages."""
    total = 0
    for m in messages:
        c = m.get("content") or ""
        total += len(c) if isinstance(c, str) else 0
        for tc in (m.get("tool_calls") or []):
            total += len(tc.get("function", {}).get("arguments", ""))
    return total


def trim_messages(messages, max_messages=MAX_MESSAGES, max_chars=MAX_TOTAL_CHARS):
    """Truncate conversation history to prevent unbounded growth.
    Keeps: system message(s) + last max_messages user/assistant/tool messages.
    Also trims individual tool outputs and enforces total char budget.
    """
    if not messages:
        return messages

    # 1. Trim oversized tool outputs (shallow copy to avoid mutating input)
    trimmed = []
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            if len(m["content"]) > MAX_TOOL_OUTPUT_CHARS:
                original_len = len(m["content"])
                m = {**m, "content": (
                    m["content"][:MAX_TOOL_OUTPUT_CHARS]
                    + f"\n\n[⚠️ PROXY TRUNCATED: original {original_len} chars, showing first {MAX_TOOL_OUTPUT_CHARS}. "
                    f"The rest was cut off — do NOT infer or guess the missing content.]"
                )}
        trimmed.append(m)
    messages = trimmed

    # 2. Separate system messages from conversation
    system_msgs = []
    conv_msgs = []
    for m in messages:
        if m.get("role") == "system":
            system_msgs.append(m)
        else:
            conv_msgs.append(m)

    # 3. Keep last N conversation messages
    if len(conv_msgs) > max_messages:
        dropped = len(conv_msgs) - max_messages
        conv_msgs = conv_msgs[-max_messages:]
        conv_msgs.insert(0, {
            "role": "system",
            "content": f"[⚠️ PROXY CONTEXT WINDOW: {dropped} earlier messages were dropped to fit context limit. "
                       f"You have NO knowledge of what was in those messages. "
                       f"Do NOT assume or guess what was discussed earlier — if unsure, ask the user.]"
        })

    result = system_msgs + conv_msgs

    # 4. Enforce total char budget (drop oldest non-system messages)
    total_chars = _estimate_chars(result)
    i = len(system_msgs)
    while total_chars > max_chars and i < len(result) - 2:
        m = result[i]
        if m.get("role") != "system":
            c = m.get("content") or ""
            total_chars -= len(c) if isinstance(c, str) else 0
            for tc in (m.get("tool_calls") or []):
                total_chars -= len(tc.get("function", {}).get("arguments", ""))
            result.pop(i)
        else:
            i += 1

    return result


def flatten_tools(tools):
    """Filter out namespace tools, flatten sub-tools to standard function format.
    Also converts Codex's flat function format to OpenAI Chat Completions format.
    Codex sends: {"type":"function","name":"...","description":"...","parameters":{...}}
    Chat Completions expects: {"type":"function","function":{"name":"...","description":"...","parameters":{...}}}
    """
    if not tools:
        return tools
    result = []
    for tool in tools:
        if tool.get("type") == "namespace":
            for sub in tool.get("tools", []):
                func = {"name": sub.get("name"), "parameters": sub.get("parameters", {})}
                if sub.get("description"):
                    func["description"] = sub["description"]
                result.append({"type": "function", "function": func})
        elif tool.get("type") == "function":
            if "function" in tool:
                result.append(tool)
            else:
                func = {"name": tool.get("name"), "parameters": tool.get("parameters", {})}
                if tool.get("description"):
                    func["description"] = tool["description"]
                if tool.get("strict") is not None:
                    func["strict"] = tool["strict"]
                result.append({"type": "function", "function": func})
    return result


def convert_tool_choice(tc):
    """Convert Responses API tool_choice to Chat Completions format.
    Responses: {"type": "auto"} or {"type": "function", "name": "foo"}
    Chat Completions: "auto" or {"type": "function", "function": {"name": "foo"}}
    """
    if not tc:
        return None
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        t = tc.get("type")
        if t in ("auto", "none", "required"):
            return t
        if t == "function":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
    return tc


def responses_to_chat(req_body):
    """Convert Responses API request to Chat Completions format."""
    messages = []

    instructions = req_body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    inp = req_body.get("input", "")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                item_type = item.get("type")

                if item_type == "function_call":
                    call_id = item.get("call_id", item.get("id", ""))
                    func_name = item.get("name", "")
                    func_args = item.get("arguments", "{}")

                    if messages and messages[-1].get("role") == "assistant":
                        assistant_msg = messages[-1]
                    else:
                        assistant_msg = {"role": "assistant", "content": None, "tool_calls": []}
                        messages.append(assistant_msg)

                    if "tool_calls" not in assistant_msg:
                        assistant_msg["tool_calls"] = []

                    assistant_msg["tool_calls"].append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": func_name,
                            "arguments": func_args
                        }
                    })
                    continue

                if item_type == "function_call_output":
                    call_id = item.get("call_id", item.get("id", ""))
                    output = item.get("output", "")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output
                    })
                    continue

                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    texts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                            texts.append(c.get("text", ""))
                        elif isinstance(c, str):
                            texts.append(c)
                    content = "\n".join(texts)
                messages.append({"role": role, "content": content})

    chat_req = {
        "model": req_body.get("model"),
        "messages": messages,
    }

    tools = req_body.get("tools")
    if tools:
        chat_req["tools"] = flatten_tools(tools)

    tool_choice = req_body.get("tool_choice")
    if tool_choice:
        chat_req["tool_choice"] = convert_tool_choice(tool_choice)

    max_tokens = req_body.get("max_output_tokens") or req_body.get("max_tokens")
    if max_tokens:
        chat_req["max_tokens"] = max_tokens

    temp = req_body.get("temperature")
    if temp is not None:
        chat_req["temperature"] = temp

    reasoning_effort = req_body.get("reasoning_effort")
    if reasoning_effort:
        chat_req["reasoning_effort"] = reasoning_effort

    if req_body.get("stream"):
        chat_req["stream"] = True

    orig_count = len(chat_req["messages"])
    orig_chars = _estimate_chars(chat_req["messages"])
    chat_req["messages"] = trim_messages(chat_req["messages"])
    new_count = len(chat_req["messages"])
    new_chars = _estimate_chars(chat_req["messages"])
    if orig_count != new_count or orig_chars != new_chars:
        logger.info("Trimmed: %d→%d msgs, %d→%d chars", orig_count, new_count, orig_chars, new_chars)

    return chat_req


def chat_to_responses(chat_resp, model):
    """Convert Chat Completions response to Responses API format."""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    output = []
    usage_in = chat_resp.get("usage", {}).get("prompt_tokens", 0)
    usage_out = chat_resp.get("usage", {}).get("completion_tokens", 0)

    for choice in chat_resp.get("choices", []):
        msg = choice.get("message", {})

        reasoning = msg.get("reasoning_content")
        if reasoning:
            output.append({
                "type": "reasoning",
                "id": f"rs_{uuid.uuid4().hex[:16]}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": reasoning, "annotations": []}]
            })

        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:16]}"
                output.append({
                    "type": "function_call",
                    "id": tc_id,
                    "call_id": tc_id,
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", "{}"),
                    "status": "completed"
                })

        text = msg.get("content", "")
        if text:
            output.append({
                "type": "message",
                "id": uuid.uuid4().hex[:32],
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}]
            })

    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "model": model,
        "output": output,
        "status": "completed",
        "usage": {
            "input_tokens": usage_in,
            "output_tokens": usage_out,
            "total_tokens": usage_in + usage_out
        }
    }


@app.post("/v1/responses")
async def responses_endpoint(request: Request):
    try:
        body = await request.json()
        inp = body.get('input')
        inp_len = len(inp) if isinstance(inp, list) else len(str(inp))
        logger.info("input=%s(%d) model=%s", type(inp).__name__, inp_len, body.get('model', '?'))

        model = body.get("model", "mimo-v2.5-pro")
        t_start = time.time()
        chat_req = responses_to_chat(body)
        msg_count = len(chat_req.get('messages', []))
        tool_count = len(chat_req.get('tools', []))
        logger.info("%d msgs, %d tools → upstream (stream=%s)", msg_count, tool_count, chat_req.get('stream', False))

        headers = {
            "Authorization": f"Bearer {MIMO_KEY}",
            "Content-Type": "application/json"
        }

        if chat_req.get("stream"):
            logger.info("Streaming request, forwarding to upstream...")
            logger.debug("Tools: %s", json.dumps(chat_req.get('tools', []), ensure_ascii=False)[:500])
            return StreamingResponse(
                stream_response(chat_req, headers, model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        t0 = time.time()
        resp = await _http_client.post(f"{MIMO_BASE}/chat/completions", json=chat_req, headers=headers)
        logger.info("Upstream %.1fs (status=%d)", time.time() - t0, resp.status_code)

        if resp.status_code != 200:
            logger.error("Upstream error: %d %s", resp.status_code, resp.text[:300])
            return {"error": {"message": resp.text, "code": resp.status_code}}

        chat_resp = resp.json()
        responses_resp = chat_to_responses(chat_resp, model)
        logger.info("Total %.1fs, %d output items", time.time() - t_start, len(responses_resp.get('output', [])))
        return responses_resp
    except Exception as e:
        logger.error("ERROR: %s", e)
        logger.error(traceback.format_exc())
        return {"error": {"message": str(e), "code": 500}}


async def stream_response(chat_req, headers, model):
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    output_items = []
    current_text_item = None
    text_chunks: list[str] = []
    tool_call_items: dict[int, dict] = {}  # index -> {id, type, output_index, name, arguments}
    tool_call_id_map: dict[int, str] = {}  # index -> first-seen call_id

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model, 'status': 'in_progress'}})}\n\n"

    t0 = time.time()
    async with _http_client.stream("POST", f"{MIMO_BASE}/chat/completions", json=chat_req, headers=headers) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            logger.error("Upstream stream error: %d %s", resp.status_code, body[:300])
            yield f"data: {json.dumps({'error': {'message': 'Upstream stream error', 'code': resp.status_code}})}\n\n"
            return

        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})

                content = delta.get("content")
                if content:
                    if current_text_item is None:
                        current_text_item = {
                            "id": uuid.uuid4().hex[:32],
                            "type": "message",
                            "output_index": len(output_items)
                        }
                        output_items.append(current_text_item)
                        text_chunks.clear()
                        yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': current_text_item['output_index'], 'item': {'id': current_text_item['id'], 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}})}\n\n"
                        yield f"data: {json.dumps({'type': 'response.content_part.added', 'output_index': current_text_item['output_index'], 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"

                    text_chunks.append(content)
                    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'output_index': current_text_item['output_index'], 'content_index': 0, 'delta': content})}\n\n"
                    await asyncio.sleep(0)

                tool_calls = delta.get("tool_calls") or []
                for tc in tool_calls:
                    tc_index = tc.get("index", 0)
                    call_id = tc.get("id") or ""
                    func = tc.get("function", {})
                    func_name = func.get("name", "")
                    func_args = func.get("arguments", "")

                    if tc_index not in tool_call_items:
                        tool_call_id_map[tc_index] = call_id or f"call_{uuid.uuid4().hex[:16]}"

                        if current_text_item is not None:
                            full_text = "".join(text_chunks)
                            yield f"data: {json.dumps({'type': 'response.content_part.done', 'output_index': current_text_item['output_index'], 'content_index': 0, 'part': {'type': 'output_text', 'text': full_text, 'annotations': []}})}\n\n"
                            yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': current_text_item['output_index'], 'item': {'id': current_text_item['id'], 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]}})}\n\n"
                            current_text_item = None

                        real_call_id = tool_call_id_map[tc_index]
                        fc_item = {
                            "id": real_call_id,
                            "type": "function_call",
                            "output_index": len(output_items),
                            "name": func_name,
                            "arguments": ""
                        }
                        tool_call_items[tc_index] = fc_item
                        output_items.append(fc_item)
                        yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': fc_item['output_index'], 'item': {'id': real_call_id, 'type': 'function_call', 'status': 'in_progress', 'name': func_name, 'call_id': real_call_id, 'arguments': ''}})}\n\n"

                    if func_args:
                        fc_item = tool_call_items[tc_index]
                        fc_item["arguments"] += func_args
                        yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'output_index': fc_item['output_index'], 'call_id': fc_item['id'], 'delta': func_args})}\n\n"
                        await asyncio.sleep(0)

                finish_reason = choice.get("finish_reason")
                if finish_reason == "tool_calls":
                    if current_text_item is not None:
                        full_text = "".join(text_chunks)
                        yield f"data: {json.dumps({'type': 'response.content_part.done', 'output_index': current_text_item['output_index'], 'content_index': 0, 'part': {'type': 'output_text', 'text': full_text, 'annotations': []}})}\n\n"
                        yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': current_text_item['output_index'], 'item': {'id': current_text_item['id'], 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]}})}\n\n"
                        current_text_item = None

                    for tc_index, fc_item in tool_call_items.items():
                        real_call_id = fc_item["id"]
                        yield f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'output_index': fc_item['output_index'], 'call_id': real_call_id, 'arguments': fc_item['arguments']})}\n\n"
                        yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': fc_item['output_index'], 'item': {'id': real_call_id, 'type': 'function_call', 'status': 'completed', 'name': fc_item['name'], 'call_id': real_call_id, 'arguments': fc_item['arguments']}})}\n\n"
                    await asyncio.sleep(0)

            except json.JSONDecodeError:
                continue

    logger.info("Stream completed in %.1fs", time.time() - t0)

    if current_text_item is not None:
        full_text = "".join(text_chunks)
        yield f"data: {json.dumps({'type': 'response.content_part.done', 'output_index': current_text_item['output_index'], 'content_index': 0, 'part': {'type': 'output_text', 'text': full_text, 'annotations': []}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': current_text_item['output_index'], 'item': {'id': current_text_item['id'], 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]}})}\n\n"

    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model, 'status': 'completed'}})}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
async def list_models():
    return {
        "data": [{"id": "mimo-v2.5-pro", "object": "model", "owned_by": "xiaomi"}],
        "object": "list"
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop installed")
    except ImportError:
        logger.info("uvloop not available, using default asyncio loop")
    uvicorn.run(app, host="0.0.0.0", port=4000, timeout_keep_alive=65)
