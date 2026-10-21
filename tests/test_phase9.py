# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Phase 9 功能测试：模型元信息丰富 / 批量推理 / WebSocket 聊天 / 插件框架。

全部使用 mock，不进行任何真实网络调用。
"""
import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.core.config import load_config
from app.core.registry import Registry, ModelEntry
from app.core.lifecycle import LifecycleManager
from app.core.selector import ModelSelector
from app.plugins import load_plugins
from app.plugins.base import BasePlugin
from app.plugins.builtin import RequestLoggerPlugin, ResponseTransformerPlugin
from app import main as app_main

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


@pytest.fixture
def cfg():
    return load_config(CFG_PATH, load_env_files=False)


# ============================================================
# 1. 模型元信息丰富
# ============================================================
def test_model_entry_metadata_defaults():
    e = ModelEntry(id="x", provider="p")
    assert e.context_window == 0
    assert e.max_output_tokens == 0
    assert e.pricing_per_1k_tokens == {}
    assert e.supported_parameters == []


def test_registry_loads_metadata_from_config(cfg, tmp_path):
    reg = Registry(cfg, db_path=tmp_path / "meta.db")
    m = reg.get("DeepSeek-V4-Flash")
    assert m is not None
    assert m.context_window == 131072
    assert m.max_output_tokens == 8192
    assert m.pricing_per_1k_tokens == {"input": 0.0, "output": 0.0}
    assert "tools" in m.supported_parameters


def test_metadata_examples_cover_required_providers(cfg, tmp_path):
    """至少 AMD / ModelScope / Kilo / 智谱 / 火山方舟 各有一个带上下文的示例。"""
    reg = Registry(cfg, db_path=tmp_path / "meta2.db")
    samples = {
        "DeepSeek-V4-Flash": "amd-radeon",
        "Qwen/Qwen3.8-Flash-Next": "modelscope",
        "inclusionai/ling-3.0-flash-vl:free": "kilogw",
        "glm-4.7-flash": "zhipu",
        "deepseek-v4-pro-ga-260813": "ark",
    }
    for mid, prov in samples.items():
        m = reg.get(mid)
        assert m is not None and m.provider == prov, f"缺少示例 {prov}/{mid}"
        assert m.context_window > 0, f"{mid} 应有 context_window"


def test_discovery_extracts_metadata():
    item = {
        "id": "m1",
        "context_length": 128000,
        "pricing": {"prompt": "0.0", "completion": "0.0"},
        "supported_parameters": ["temperature", "tools"],
    }
    meta = LifecycleManager._extract_metadata(item)
    assert meta["context_window"] == 128000
    assert meta["pricing_per_1k_tokens"] == {"input": "0.0", "output": "0.0"}
    assert meta["supported_parameters"] == ["temperature", "tools"]


def test_discovery_extracts_metadata_alt_names():
    item = {"id": "m2", "context_window": 32768, "max_output_tokens": 4096}
    meta = LifecycleManager._extract_metadata(item)
    assert meta["context_window"] == 32768
    assert meta["max_output_tokens"] == 4096


def test_selector_long_context_weighting(cfg):
    sel = ModelSelector(cfg)
    short = ModelEntry(id="s", provider="p", capability={})
    long = ModelEntry(id="l", provider="p", capability={}, context_window=131072)
    base = 100.0
    ctx = {"task_type": "summarization"}
    assert sel.score(long, ctx, base) > sel.score(short, ctx, base)
    # 无上下文时退化为 base（历史行为不变）
    assert sel.score(long, {}, base) == base


# ============================================================
# 2. 批量推理端点
# ============================================================
class _FakeReq:
    def __init__(self, body=None, headers=None):
        self._body = body
        self.headers = headers if headers is not None else {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _MockRouter:
    def __init__(self, fail_content=()):
        self.fail = set(fail_content)
        self.calls = []

    async def route(self, body, task_ctx=None, tenant=None):
        self.calls.append((body, tenant))
        msgs = body.get("messages", [])
        content = msgs[-1].get("content", "") if msgs else ""
        if content in self.fail:
            from app.core.router import AllExhausted
            raise AllExhausted("boom")
        return {
            "id": "mock",
            "model": "mock",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def test_batch_completions():
    body = {
        "requests": [
            {"messages": [{"role": "user", "content": "a"}]},
            {"messages": [{"role": "user", "content": "b"}]},
            {"messages": [{"role": "user", "content": "FAIL"}]},
        ],
        "max_concurrent": 2,
    }
    router = _MockRouter(fail_content=("FAIL",))
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = router
        res = asyncio.run(app_main.batch_completions(_FakeReq(body)))
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)

    results = res["results"]
    assert len(results) == 3
    assert results[0]["index"] == 0
    assert results[0]["response"]["choices"][0]["message"]["content"] == "a"
    assert results[1]["index"] == 1
    assert results[1]["response"]["choices"][0]["message"]["content"] == "b"
    assert results[2]["index"] == 2
    assert "error" in results[2]


def test_batch_completions_stream_not_supported():
    class StreamRouter:
        async def route(self, body, task_ctx=None, tenant=None):
            async def gen():
                yield b"data: [DONE]\n\n"
            return gen()

    body = {"requests": [{"messages": [{"role": "user", "content": "hi"}], "stream": True}]}
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = StreamRouter()
        res = asyncio.run(app_main.batch_completions(_FakeReq(body)))
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)
    assert "error" in res["results"][0]


# ============================================================
# 3. WebSocket 聊天端点
# ============================================================
class _FakeWS:
    def __init__(self, messages, headers=None):
        self._msgs = list(messages)
        self.headers = headers if headers is not None else {}
        self.sent = []
        self.closed = None

    async def accept(self):
        pass

    async def receive_text(self):
        if self._msgs:
            return self._msgs.pop(0)
        from fastapi import WebSocketDisconnect
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, t):
        self.sent.append(t)

    async def close(self, code=1000):
        self.closed = code


def test_ws_chat_nonstream():
    router = _MockRouter()
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = router
        ws = _FakeWS([json.dumps({"messages": [{"role": "user", "content": "hi"}]})])
        asyncio.run(app_main._handle_ws_chat(ws))
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)
    assert ws.closed is None
    assert any(("hi" in t and "choices" in t) for t in ws.sent)


def test_ws_chat_stream_passthrough():
    class StreamRouter:
        async def route(self, body, task_ctx=None, tenant=None):
            async def gen():
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                yield b"data: [DONE]\n\n"
            return gen()

    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = StreamRouter()
        ws = _FakeWS([json.dumps({"messages": [{"role": "user", "content": "hi"}],
                                  "stream": True})])
        asyncio.run(app_main._handle_ws_chat(ws))
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)
    joined = "".join(ws.sent)
    assert "delta" in joined and "[DONE]" in joined


def test_ws_chat_unauthorized_closes():
    class _Cfg:
        access_key = "secret"

    old = dict(app_main.app_state)
    try:
        app_main.app_state["config"] = _Cfg()
        ws = _FakeWS([], headers={"authorization": "Bearer wrong"})
        asyncio.run(app_main._handle_ws_chat(ws))
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)
    assert ws.closed == 1008


def test_ws_chat_authorized_ok():
    class _Cfg:
        access_key = "secret"

    old = dict(app_main.app_state)
    try:
        app_main.app_state["config"] = _Cfg()
        app_main.app_state["router"] = _MockRouter()
        ws = _FakeWS([json.dumps({"messages": [{"role": "user", "content": "hi"}]})],
                     headers={"authorization": "Bearer secret"})
        asyncio.run(app_main._handle_ws_chat(ws))
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)
    assert ws.closed is None
    assert any("hi" in t for t in ws.sent)


# ============================================================
# 4. 插件框架
# ============================================================
def test_load_plugins_from_config(cfg):
    plugins = load_plugins(cfg)
    names = {p.name for p in plugins}
    assert "request-logger" in names
    assert "response-transformer" in names
    assert all(isinstance(p, BasePlugin) for p in plugins)


def test_load_plugins_skips_bad_module():
    class _Cfg:
        plugins = [{"name": "bad", "module": "no.such.module", "class": "X"}]

    assert load_plugins(_Cfg()) == []


def test_base_plugin_hooks_noop():
    p = BasePlugin({})
    asyncio.run(p.on_request(None, {}))
    asyncio.run(p.on_response(None, {}))
    asyncio.run(p.on_error(Exception("boom"), {}))


def test_response_transformer_strips_thinking():
    p = ResponseTransformerPlugin({"strip_keys": ["reasoning_content", "thinking"]})
    resp = {
        "id": "x",
        "reasoning_content": "hidden",
        "choices": [{"message": {"content": "ok", "thinking": "chain"}}],
    }
    asyncio.run(p.on_response(resp, {}))
    assert "reasoning_content" not in resp
    assert "thinking" not in resp["choices"][0]["message"]
    assert resp["choices"][0]["message"]["content"] == "ok"


def test_request_logger_plugin_hooks():
    p = RequestLoggerPlugin({})

    class _R:
        method = "POST"

    ctx = {}
    asyncio.run(p.on_request(_R(), ctx))
    assert "_started" in ctx
    asyncio.run(p.on_response({"x": 1}, ctx))
    asyncio.run(p.on_error(Exception("boom"), ctx))
