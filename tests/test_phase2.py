# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Phase 2：Anthropic /v1/messages 协议转换与端点测试（无真实网络）。"""
import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.anthropic import (anthropic_to_openai, openai_to_anthropic,
                           openai_stream_to_anthropic, anthropic_error)
from app.core.config import load_config
from app.core.registry import Registry
from app.core.quota import QuotaEngine
from app.core.router import Router
from app import main as app_main

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


class _FakeReq:
    def __init__(self, body=None, query=None):
        self._body = body
        self.query_params = query if query is not None else {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# ---------- 请求转换 ----------
def test_request_conversion_system_and_text_blocks():
    req = {
        "model": "claude-x",
        "system": [{"type": "text", "text": "你是助手"}],
        "max_tokens": 256,
        "temperature": 0.5,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "你好"},
                {"type": "text", "text": "世界"},
            ]},
        ],
    }
    body = anthropic_to_openai(req)
    assert body["model"] == "claude-x"
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}
    assert body["messages"][1] == {"role": "user", "content": "你好世界"}
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.5


def test_request_conversion_string_system_and_content():
    req = {"system": "sys", "messages": [{"role": "user", "content": "hi"}]}
    body = anthropic_to_openai(req)
    assert body["messages"][0]["content"] == "sys"
    assert body["messages"][1]["content"] == "hi"


def test_request_conversion_image_block():
    req = {"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png", "data": "AAAA"}}]}]}
    body = anthropic_to_openai(req)
    parts = body["messages"][0]["content"]
    assert isinstance(parts, list)
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_request_conversion_stop_and_stream():
    req = {"messages": [{"role": "user", "content": "x"}],
           "stop_sequences": ["STOP"], "stream": True}
    body = anthropic_to_openai(req)
    assert body["stop"] == ["STOP"]
    assert body["stream"] is True


# ---------- 响应转换 ----------
def test_response_conversion_length():
    oai = {"id": "chatcmpl-1", "model": "x",
           "choices": [{"message": {"role": "assistant", "content": "嗨"},
                        "finish_reason": "length"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    out = openai_to_anthropic(oai, model="free-router")
    assert out["id"] == "chatcmpl-1"
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "嗨"}]
    assert out["stop_reason"] == "max_tokens"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 4}


def test_response_conversion_stop_and_defaults():
    oai = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
           "usage": {}}
    out = openai_to_anthropic(oai, model="m")
    assert out["stop_reason"] == "end_turn"
    assert out["usage"]["input_tokens"] == 0 and out["usage"]["output_tokens"] == 0
    assert out["content"][0]["text"] == "x"


def test_anthropic_error_shape():
    err = anthropic_error("boom", "overloaded_error")
    assert err["type"] == "error"
    assert err["error"]["type"] == "overloaded_error"
    assert err["error"]["message"] == "boom"


# ---------- 端点 ----------
def test_messages_endpoint_nonstream(tmp_path):
    cfg = load_config(CFG_PATH)
    reg = Registry(cfg, db_path=tmp_path / "m.db")
    reg._models = {}
    # 本测试验证 Anthropic 协议转换，不涉凭证：声明 zhipu 可用
    cfg.providers["zhipu"].key_env = ""
    cfg.providers["zhipu"].api_key = "test-key"
    reg.add("m1", "zhipu")
    quota = QuotaEngine(cfg, db_path=tmp_path / "m.db")

    class P:
        async def chat(self, body, model):
            return {"choices": [{"message": {"content": "pong"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

    r = Router(cfg, reg, quota, {"zhipu": P()})
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = r
        req = _FakeReq({"model": "claude-3", "max_tokens": 100,
                        "messages": [{"role": "user", "content": "ping"}]})
        resp = asyncio.run(app_main.anthropic_messages(req))
        data = json.loads(resp.body)
        assert data["type"] == "message"
        assert data["content"][0]["text"] == "pong"
        assert data["usage"] == {"input_tokens": 3, "output_tokens": 1}
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)


def test_messages_endpoint_all_exhausted(tmp_path):
    cfg = load_config(CFG_PATH)
    reg = Registry(cfg, db_path=tmp_path / "e.db")
    reg._models = {}                       # 无可用模型 → AllExhausted
    quota = QuotaEngine(cfg, db_path=tmp_path / "e.db")
    r = Router(cfg, reg, quota, {})
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = r
        resp = asyncio.run(app_main.anthropic_messages(
            _FakeReq({"messages": [{"role": "user", "content": "x"}]})))
        assert resp.status_code == 503
        body = json.loads(resp.body)
        assert body["type"] == "error"
        assert body["error"]["type"] == "overloaded_error"
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)


def test_messages_endpoint_bad_json():
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = object()   # 只需非 None
        resp = asyncio.run(app_main.anthropic_messages(_FakeReq(None)))
        assert resp.status_code == 400
        assert json.loads(resp.body)["error"]["type"] == "invalid_request_error"
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)


# ---------- 流式转换 ----------
def test_stream_conversion_to_anthropic_events():
    async def openai_gen():
        for line in ('data: {"choices":[{"delta":{"content":"He"}}]}\n\n',
                     'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
                     'data: [DONE]\n\n'):
            yield line.encode("utf-8")

    async def run():
        agen = openai_stream_to_anthropic(openai_gen(), model="m")
        out = []
        async for chunk in agen:
            out.append(chunk)
        return "".join(out)

    text = asyncio.run(run())
    assert "event: message_start" in text
    assert "event: content_block_start" in text
    assert '"text_delta"' in text
    assert "He" in text and "llo" in text
    assert "event: message_stop" in text