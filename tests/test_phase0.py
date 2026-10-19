"""Phase 0 修复的单元测试：用量写穿 / 流式透传 / 密钥加密 / admin 参数对齐。

全部使用 mock，不进行任何真实网络调用。
"""
import sys
import asyncio
import sqlite3
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import httpx

from app.core.config import load_config, ProviderCfg
from app.core.registry import Registry
from app.core.quota import QuotaEngine
from app.core.router import Router, ProviderError, AllExhausted
from app.core import crypto
from app.providers import OpenAICompatAdapter
from app import main as app_main

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


@pytest.fixture
def cfg():
    return load_config(CFG_PATH)


class _FakeReq:
    """最小 Request 替身：支持 query_params 与 async json()。"""
    def __init__(self, body=None, query=None):
        self._body = body
        self.query_params = query if query is not None else {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# ============================================================
# 0.1 用量写穿（persist）
# ============================================================
def test_persist_after_record(cfg, tmp_path):
    db = tmp_path / "q.db"
    q = QuotaEngine(cfg, db_path=db)
    q.record("zhipu", "glm-4-flash", usage={"prompt_tokens": 5, "completion_tokens": 7})
    assert q._dirty is True, "record 后应标记 dirty"
    assert q.persist_if_dirty() is True
    assert q._dirty is False
    # 直读 DB 应见该行
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT * FROM usage"))
    con.close()
    assert len(rows) == 1
    r = rows[0]
    assert r["provider"] == "zhipu" and r["model"] == "glm-4-flash"
    assert r["rpd_cnt"] >= 1
    assert r["tpd_tokens"] == 12


def test_persist_noop_when_clean(cfg, tmp_path):
    q = QuotaEngine(cfg, db_path=tmp_path / "q2.db")
    assert q.persist_if_dirty() is False


def test_persist_threshold_flush(cfg, tmp_path):
    db = tmp_path / "q3.db"
    q = QuotaEngine(cfg, db_path=db, checkpoint_every=2)
    q.record("zhipu", "m")                    # pending=1，未落库
    con = sqlite3.connect(db)
    assert list(con.execute("SELECT COUNT(*) FROM usage"))[0][0] == 0
    con.close()
    q.record("zhipu", "m")                    # pending=2 → 立即落库
    con = sqlite3.connect(db)
    assert list(con.execute("SELECT COUNT(*) FROM usage"))[0][0] == 1
    con.close()


def test_cooldown_persisted(cfg, tmp_path):
    db = tmp_path / "q4.db"
    q = QuotaEngine(cfg, db_path=db)
    q.apply_cooldown("zhipu", "m", "429")
    assert q.persist_if_dirty() is True
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT * FROM cooldown"))
    con.close()
    assert len(rows) == 1 and rows[0]["reason"] == "429"


def test_checkpoint_loop_writes(cfg, tmp_path):
    db = tmp_path / "q5.db"
    q = QuotaEngine(cfg, db_path=db)

    async def run():
        q.record("zhipu", "m")
        task = asyncio.create_task(q.checkpoint_loop(interval=0.05))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    con = sqlite3.connect(db)
    assert list(con.execute("SELECT COUNT(*) FROM usage"))[0][0] == 1
    con.close()


# ============================================================
# 0.2 流式透传
# ============================================================
class _StreamProv:
    name = "stream"

    def __init__(self):
        self.calls = 0

    async def chat(self, body, model):
        self.calls += 1

        async def gen():
            for line in (b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
                         b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
                         b'data: [DONE]\n\n'):
                yield line

        return gen()


def test_router_returns_async_generator_for_stream(cfg, tmp_path):
    reg = Registry(cfg, db_path=tmp_path / "r.db")
    reg._models = {}
    # 本测试验证流式语义，不涉凭证：声明 zhipu 免 key（否则被 keyless 过滤跳过）
    cfg.providers["zhipu"].key_env = ""
    cfg.providers["zhipu"].api_key = "test-key"
    reg.add("m1", "zhipu")
    quota = QuotaEngine(cfg, db_path=tmp_path / "r.db")
    prov = _StreamProv()
    r = Router(cfg, reg, quota, {"zhipu": prov})

    async def run():
        res = await r.route({"stream": True,
                             "messages": [{"role": "user", "content": "hi"}]})
        assert hasattr(res, "__aiter__"), "流式请求应返回异步生成器"
        chunks = []
        async for c in res:
            chunks.append(c)
        return b"".join(chunks)

    out = asyncio.run(run())
    assert b"he" in out and b"llo" in out and b"[DONE]" in out
    assert prov.calls == 1
    # 流式也应记录一次请求用量（RPM/RPD）
    assert quota.is_quota_exceeded("zhipu", "m1") is False


def test_adapter_stream_via_mocktransport(cfg):
    adapter = OpenAICompatAdapter(cfg.providers["zhipu"])

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n')

    adapter._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        gen = await adapter.chat({"stream": True}, "glm-4-flash")
        assert hasattr(gen, "__aiter__")
        out = b""
        async for c in gen:
            out += c
        return out

    out = asyncio.run(run())
    assert b"[DONE]" in out and b"delta" in out


def test_adapter_stream_status_error_raises_before_iter(cfg):
    adapter = OpenAICompatAdapter(cfg.providers["zhipu"])
    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(503, content=b"down")))

    async def run():
        with pytest.raises(ProviderError):
            await adapter.chat({"stream": True}, "glm-4-flash")

    asyncio.run(run())


def test_adapter_stream_429_raises_quota(cfg):
    adapter = OpenAICompatAdapter(cfg.providers["zhipu"])
    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(429, content=b"slow")))

    async def run():
        from app.core.router import QuotaExhausted
        with pytest.raises(QuotaExhausted):
            await adapter.chat({"stream": True}, "glm-4-flash")

    asyncio.run(run())


def test_adapter_nonstream_via_mocktransport(cfg):
    adapter = OpenAICompatAdapter(cfg.providers["zhipu"])
    adapter._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})))

    res = asyncio.run(adapter.chat({"messages": []}, "glm-4-flash"))
    assert res["choices"][0]["message"]["content"] == "ok"


# ============================================================
# 0.3 密钥安全（enc: 前缀 AES / XOR 回退）
# ============================================================
def test_crypto_roundtrip():
    ct = crypto.encrypt("sk-secret-123")
    assert ct.startswith("enc:")
    assert crypto.decrypt(ct) == "sk-secret-123"
    assert crypto.maybe_decrypt("plain-key") == "plain-key"
    assert crypto.maybe_decrypt(ct) == "sk-secret-123"
    assert crypto.is_encrypted(ct) and not crypto.is_encrypted("plain")


def test_provider_cfg_enc_api_key():
    enc = crypto.encrypt("rc-realkey")
    p = ProviderCfg(name="x", base_url="http://x", api_key=enc)
    assert p.resolved_key == "rc-realkey"


def test_env_key_via_enc(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", crypto.encrypt("env-secret"))
    p = ProviderCfg(name="z", base_url="http://x", key_env="ZAI_API_KEY")
    assert p.resolved_key == "env-secret"


def test_config_yaml_all_api_keys_empty(cfg):
    assert all((p.api_key or "") == "" for p in cfg.providers.values()), \
        "config.yaml 中 api_key 必须全为空字符串"


# ============================================================
# 0.4 /admin/switch 参数对齐（幂等）
# ============================================================
def test_admin_switch_arg_order_and_idempotent(cfg, tmp_path):
    reg = Registry(cfg, db_path=tmp_path / "a.db")
    reg._models = {}
    reg.add("m1", "zhipu")
    quota = QuotaEngine(cfg, db_path=tmp_path / "a.db")
    r = Router(cfg, reg, quota, {})

    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = r
        # fr.py 传参顺序：{"model": m, "provider": p}
        res = asyncio.run(app_main.admin_switch(
            _FakeReq({"model": "m1", "provider": "zhipu"})))
        assert res["switched_to"] == {"provider": "zhipu", "model": "m1"}
        assert r._forced == ("zhipu", "m1"), "force_switch 参数顺序应为 (provider, model)"

        # 幂等：重复调用结果一致
        res2 = asyncio.run(app_main.admin_switch(
            _FakeReq({"model": "m1", "provider": "zhipu"})))
        assert res2 == res
        assert r._forced == ("zhipu", "m1")
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)


def test_admin_switch_accepts_query_params(cfg, tmp_path):
    reg = Registry(cfg, db_path=tmp_path / "aq.db")
    reg._models = {}
    reg.add("m2", "dashscope")
    quota = QuotaEngine(cfg, db_path=tmp_path / "aq.db")
    r = Router(cfg, reg, quota, {})
    old = dict(app_main.app_state)
    try:
        app_main.app_state["router"] = r
        res = asyncio.run(app_main.admin_switch(
            _FakeReq(None, query={"model": "m2", "provider": "dashscope"})))
        assert res["switched_to"] == {"provider": "dashscope", "model": "m2"}
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)