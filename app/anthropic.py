# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Anthropic Messages API <-> OpenAI Chat Completions 协议转换。

- anthropic_to_openai(req)  : 请求体转换（system / content 块 / max_tokens）
- openai_to_anthropic(resp) : 非流式响应转换（message / content / usage）
- openai_stream_to_anthropic(agen) : 流式 SSE 转换（可选）
"""
import json
import time
import uuid


# ---------- 工具 ----------
def _new_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _blocks_to_openai_content(content):
    """content 可为 str 或 Anthropic 块数组；返回 OpenAI content。

    - 纯文本块 → 合并成字符串
    - 含 image 块 → 返回 OpenAI 多模态数组
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""

    texts = []
    parts = []
    has_image = False
    for blk in content:
        if not isinstance(blk, dict):
            texts.append(str(blk))
            continue
        btype = blk.get("type")
        if btype == "text":
            texts.append(blk.get("text", ""))
            parts.append({"type": "text", "text": blk.get("text", "")})
        elif btype == "image":
            src = blk.get("source", {}) or {}
            if src.get("type") == "base64":
                media = src.get("media_type", "image/png")
                data = src.get("data", "")
                url = f"data:{media};base64,{data}"
            else:
                url = src.get("url", "")
            has_image = True
            parts.append({"type": "image_url", "image_url": {"url": url}})
        # 其它块类型（tool_use 等）暂不展开，保留文本部分

    if has_image:
        # 若既有文本又有图片，用多模态数组
        if not parts:
            parts = [{"type": "text", "text": "".join(texts)}]
        return parts
    return "".join(texts)


def _system_to_text(system) -> str:
    """system 可为 str 或块数组 → 纯文本。"""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        out = []
        for blk in system:
            if isinstance(blk, dict) and blk.get("type") in (None, "text"):
                out.append(blk.get("text", ""))
            elif isinstance(blk, str):
                out.append(blk)
        return "".join(out)
    return str(system)


# ---------- 请求转换 ----------
def anthropic_to_openai(req: dict) -> dict:
    """Anthropic Messages 请求 → OpenAI Chat Completions 请求。"""
    openai_msgs = []

    sys_text = _system_to_text(req.get("system"))
    if sys_text:
        openai_msgs.append({"role": "system", "content": sys_text})

    for m in req.get("messages", []) or []:
        role = m.get("role", "user")
        content = _blocks_to_openai_content(m.get("content"))
        openai_msgs.append({"role": role, "content": content})

    body = {
        "messages": openai_msgs,
        "max_tokens": req.get("max_tokens", 1024),
    }
    if req.get("model"):
        body["model"] = req["model"]
    if "temperature" in req:
        body["temperature"] = req["temperature"]
    if "top_p" in req:
        body["top_p"] = req["top_p"]
    if req.get("stop_sequences"):
        body["stop"] = req["stop_sequences"]
    if req.get("stream"):
        body["stream"] = True
    return body


# ---------- 响应转换 ----------
_FINISH_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    None: "end_turn",
}


def openai_to_anthropic(resp: dict, model: str = None) -> dict:
    """OpenAI Chat Completions 响应 → Anthropic Messages 响应。"""
    choices = resp.get("choices") or [{}]
    choice = choices[0] or {}
    message = choice.get("message") or {}
    text = message.get("content")
    if text is None:
        text = ""

    usage = resp.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens", 0) or 0)
    out_tok = int(usage.get("completion_tokens", 0) or 0)

    finish = choice.get("finish_reason")
    stop_reason = _FINISH_MAP.get(finish, "end_turn")

    return {
        "id": resp.get("id") or _new_id(),
        "type": "message",
        "role": "assistant",
        "model": model or resp.get("model", "free-router"),
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def anthropic_error(message: str, err_type: str = "api_error") -> dict:
    """Anthropic 错误体。"""
    return {"type": "error", "error": {"type": err_type, "message": message}}


# ---------- 流式转换（可选） ----------
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def openai_stream_to_anthropic(agen, model: str = "free-router"):
    """把 OpenAI SSE 字节流转换为 Anthropic SSE 事件流（异步生成器）。"""
    async def _conv():
        msg_id = _new_id()
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        yield _sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        out_tokens = 0
        async for raw in agen:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
            for line in str(raw).splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    j = json.loads(payload)
                except Exception:
                    continue
                delta = (j.get("choices") or [{}])[0].get("delta", {}) or {}
                text = delta.get("content")
                if text:
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    })
                u = j.get("usage")
                if u:
                    out_tokens = int(u.get("completion_tokens", out_tokens) or out_tokens)
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": out_tokens},
        })
        yield _sse("message_stop", {"type": "message_stop"})

    return _conv()