# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""用户策略回归测试：
1. 高质量大模型优先（tier boost）
2. 限流自动顺延其他模型
3. 额度隔天自动恢复（is_quota_exceeded 跨天惰性归零）
4. 免费小模型只做兜底（low tier 排最后）
5. free_only 提供商只收免费模型（付费模型不进自动池）
"""
import sys, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.core.config import load_config
from app.core.registry import Registry, ModelEntry
from app.core.quota import QuotaEngine
from app.core.router import Router
from app.core.lifecycle import LifecycleManager, _BILLING_RE

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(CFG_PATH, load_env_files=False)
    reg = Registry(cfg, db_path=tmp_path / "t.db")
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    return cfg, reg, quota


class OK:
    def __init__(self, name): self.name = name; self.calls = 0
    async def chat(self, body, model):
        self.calls += 1
        return {"id": model, "choices": [{"message": {"content": self.name}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def test_tier_priority_high_first(env, keyless):
    """高质量模型优先被选中；low 小模型只作兜底。"""
    cfg, reg, quota = env
    keyless(cfg, "amd-radeon", "modelscope")
    reg._models = {}
    reg._models["big"] = ModelEntry(id="big", provider="amd-radeon",
                                    capability={"score": 70}, tier="high")
    reg._models["small"] = ModelEntry(id="small", provider="modelscope",
                                      capability={"score": 75}, tier="low")
    provs = {"amd-radeon": OK("amd"), "modelscope": OK("ms")}
    r = Router(cfg, reg, quota, provs)
    resp = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    # small 分数虽高(75>70)但 tier=low；big tier=high 必须赢
    assert resp["choices"][0]["message"]["content"] == "amd"
    assert provs["modelscope"].calls == 0   # 小模型根本没被调用（仅兜底）


def test_low_tier_fallback_when_high_down(env, keyless):
    """high 全部失败时，low 兜底保证任务不中断。"""
    cfg, reg, quota = env
    keyless(cfg, "amd-radeon", "modelscope")
    reg._models = {}
    reg._models["big"] = ModelEntry(id="big", provider="amd-radeon",
                                    capability={"score": 70}, tier="high")
    reg._models["small"] = ModelEntry(id="small", provider="modelscope",
                                      capability={"score": 40}, tier="low")
    from app.core.router import ProviderError
    class Failing:
        async def chat(self, body, model):
            raise ProviderError("down", status_code=503)
    provs = {"amd-radeon": Failing(), "modelscope": OK("ms")}
    r = Router(cfg, reg, quota, provs)
    resp = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    assert resp["choices"][0]["message"]["content"] == "ms"


def test_quota_recovers_next_day(env):
    """额度耗尽后跨天自动恢复：is_quota_exceeded 惰性归零。"""
    cfg, reg, quota = env
    import app.core.quota as qmod
    p, m = "amd-radeon", "DeepSeek-V4-Flash"
    # 打满 RPD
    for _ in range(cfg.quota.rpd_limit):
        quota.record(p, m)
    assert quota.is_quota_exceeded(p, m), "打满后应判定超额"
    # 时间前进一天（日额度与 RPM/TPM 窗口同时过期）
    orig_day, orig_ts = qmod._utc_day, qmod._utc_ts
    import datetime, time
    tomorrow = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    qmod._utc_day = lambda: tomorrow
    qmod._utc_ts = lambda: time.time() + 86400
    try:
        assert not quota.is_quota_exceeded(p, m), "跨天后额度必须自动恢复"
        quota.record(p, m)   # 恢复后可继续记录
    finally:
        qmod._utc_day, qmod._utc_ts = orig_day, orig_ts


def test_free_only_discovery_filter(env):
    """free_only 提供商：付费模型不进池，:free 与 kilo-auto/free 进池。"""
    cfg, reg, quota = env
    assert cfg.providers["kilogw"].free_only is True
    lc = LifecycleManager(cfg, reg, quota, {})
    data = {"data": [
        {"id": "anthropic/claude-opus-5.5"},              # 付费 → 拒绝
        {"id": "openai/gpt-6.1-sol"},                     # 付费 → 拒绝
        {"id": "nvidia/nemotron-3-ultra-550b-a55b:free"},  # 免费 → 接受
        {"id": "kilo-auto/free"},                          # 自动免费 → 接受
        {"id": "kilo-auto/balanced"},                      # 不保证免费 → 拒绝
        {"id": "inclusionai/ling-3.0-flash-vl:free"},      # 静态清单 → 接受
    ]}
    lc._apply_discovery("kilogw", data)
    ids = {m.id for m in reg.list_active() if m.provider == "kilogw"}
    assert "anthropic/claude-opus-5.5" not in ids
    assert "openai/gpt-6.1-sol" not in ids
    assert "kilo-auto/balanced" not in ids
    assert "nvidia/nemotron-3-ultra-550b-a55b:free" in ids
    assert "kilo-auto/free" in ids
    assert "inclusionai/ling-3.0-flash-vl:free" in ids
    # tier 推断：ultra 550b → high
    m = reg.get("nvidia/nemotron-3-ultra-550b-a55b:free")
    assert m.tier == "high"


def test_tier_inference():
    it = LifecycleManager._infer_tier
    assert it("nvidia/nemotron-3-ultra-550b-a55b:free") == "high"
    assert it("nvidia/nemotron-3-super-120b-a12b:free") == "high"
    assert it("qwen/qwen3.8-27b:free") == "medium"
    assert it("poolside/laguna-s-2.1:free") in ("medium", "low")
    assert it("nex-agi/nex-n2.5-mini:free") == "low"
    assert it("liquid/lfm-2.5-2.6b:free") == "low"
    assert it("MiniCPM5-2B") == "low"
    assert it("deepseek-ai/DeepSeek-V4.1-Flash") == "medium"


def test_paid_deepseek_never_in_pool(env):
    """收费 deepseek（api.deepseek.com 付费模型）不在 free-router 池中。"""
    cfg, reg, quota = env
    assert "deepseek" not in cfg.providers, "free-router 不应配置付费 deepseek 提供商"


def test_unconfigured_key_provider_skipped(env, keyless):
    """需要 key 但未配置的提供商被直接跳过，不浪费 failover 名额。"""
    cfg, reg, quota = env
    keyless(cfg, "kilogw")
    reg._models = {}
    # modelscope 需要一个不存在的环境变量 → 视为未配置
    cfg.providers["modelscope"].key_env = "NOPE_MISSING_KEY_ENV_XYZ"
    reg._models["ms-m"] = ModelEntry(id="ms-m", provider="modelscope",
                                     capability={"score": 99}, tier="high")
    # kilogw 免 key → 可用
    reg._models["kilo-m"] = ModelEntry(id="kilo-m", provider="kilogw",
                                       capability={"score": 10}, tier="low")
    provs = {"modelscope": OK("ms"), "kilogw": OK("kilo")}
    r = Router(cfg, reg, quota, provs)
    resp = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    assert resp["choices"][0]["message"]["content"] == "kilo"
    assert provs["modelscope"].calls == 0, "无 key 提供商不应被调用"


def test_key_env_multi_name_and_empty_placeholder(tmp_path, monkeypatch):
    """key_env 支持多候选（兼容不同工具键名）；.env 空占位不阻断兜底来源。"""
    from app.core import config as cfgmod
    import os
    monkeypatch.setenv("FR_KEY_REAL", "sk-real")
    monkeypatch.delenv("FR_KEY_EMPTY", raising=False)
    monkeypatch.delenv("FR_KEY_OTHER", raising=False)
    # 多候选：跳过空/缺失，取第一个非空
    assert cfgmod._resolve_key(["FR_KEY_EMPTY", "FR_KEY_REAL"], "") == "sk-real"
    assert cfgmod._resolve_key("FR_KEY_EMPTY,FR_KEY_REAL", "") == "sk-real"
    # 空占位不写入环境（否则 setdefault 会阻断后续 .env 兜底）
    p = tmp_path / ".env"
    p.write_text("FR_KEY_OTHER=\nFR_KEY_REAL2=sk-two\n", encoding="utf-8")
    cfgmod._load_dotenv(p)
    assert "FR_KEY_OTHER" not in os.environ
    assert os.environ.get("FR_KEY_REAL2") == "sk-two"


def test_gateway_200_error_payload_is_failure(tmp_path, monkeypatch):
    """HTTP 200 但返回 error 体（无 choices）= 上游失败：必须抛错以触发顺延，
    不能当成功透传给调用方（Kilo/OpenRouter 风格网关的静默失败）。"""
    import httpx, asyncio
    from app.providers import OpenAICompatAdapter
    from app.core.config import ProviderCfg
    from app.core.router import ProviderError

    def handler(req):
        return httpx.Response(200, json={
            "id": "gen-x",
            "error": {"message": "Upstream error: limit reached (16/16)",
                      "code": 502}})

    transport = httpx.MockTransport(handler)
    pcfg = ProviderCfg(name="kilogw", base_url="https://x", key_env="", api_key="")
    ad = OpenAICompatAdapter(pcfg)
    monkeypatch.setattr(ad, "_client",
                        lambda: httpx.AsyncClient(transport=transport, timeout=5))
    try:
        asyncio.run(ad.chat({"messages": [{"role": "user", "content": "x"}]}, "m"))
        assert False, "200+error 体应抛 ProviderError"
    except ProviderError as e:
        assert "upstream error payload" in str(e)
        assert getattr(e, "status_code", None) == 502
