# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""单元测试：路由/额度/生命周期核心逻辑（无真实网络）。"""
import sys, os, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.core.config import load_config
from app.core.registry import Registry
from app.core.quota import QuotaEngine
from app.core.router import Router, ProviderError, AllExhausted

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(CFG_PATH, load_env_files=False)
    reg = Registry(cfg, db_path=tmp_path / "t.db")
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    return cfg, reg, quota


class MockProv:
    def __init__(self, name, fail_first=0, status=503):
        self.name = name; self.fail_first = fail_first
        self.calls = 0; self.status = status
    async def chat(self, body, model):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ProviderError(f"sim {self.status}", status_code=self.status)
        return {"id": model, "choices": [{"message": {"content": f"{self.name}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


def test_cross_provider_failover(env, keyless):
    cfg, reg, quota = env
    # 每家只留一个模型，保证 provider 级 fail 语义确定（避免同商多模型干扰）
    reg._models = {}
    keyless(cfg, "amd-radeon", "modelscope")
    reg.add("amd-m", "amd-radeon")
    reg.add("ms-m", "modelscope")
    provs = {"amd-radeon": MockProv("amd", fail_first=1),
             "modelscope": MockProv("ms")}
    r = Router(cfg, reg, quota, provs)
    resp = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    assert provs["amd-radeon"].calls == 1
    assert provs["modelscope"].calls == 1
    assert resp["choices"][0]["message"]["content"] == "ms"


def test_vision_filter(env):
    cfg, reg, quota = env
    provs = {"v": MockProv("v"), "nv": MockProv("nv")}
    r = Router(cfg, reg, quota, provs)
    body = {"messages": [{"role": "user",
                          "content": [{"type": "image_url",
                                       "image_url": {"url": "x"}}]}]}
    # 只保留视觉模型
    candidates = r._filter_capability(reg.list_active(), body)
    # 断言所有候选都有 vision 能力
    for c, _ in candidates:
        assert c.capability.get("vision"), f"{c.id} 应具备视觉能力"


def test_quota_exceeded(env):
    cfg, reg, quota = env
    q = quota
    for _ in range(1000):   # 触发 RPD 上限
        q.record("amd-radeon", "DeepSeek-V4-Flash")
    assert q.is_quota_exceeded("amd-radeon", "DeepSeek-V4-Flash")


def test_cooldown_escalation(env):
    cfg, reg, quota = env
    quota.apply_cooldown("amd-radeon", "m1", "429")
    first = quota.cooldown_snapshot().get("amd-radeon/m1")["retry_after"]
    quota.apply_cooldown("amd-radeon", "m1", "429")
    quota.apply_cooldown("amd-radeon", "m1", "429")
    cd = quota.cooldown_snapshot().get("amd-radeon/m1")
    assert cd["hits"] == 3
    # 递增式冷却：首档为 cooldown_429，之后按 escalate 序列升档并封顶。
    # 策略（用户要求）：隔离时间要短，模型恢复后尽快回到候选池，不长时间空等。
    levels = [cfg.quota.cooldown_429] + list(cfg.quota.cooldown_429_escalate)
    assert first == cfg.quota.cooldown_429
    assert cd["retry_after"] == levels[min(cd["hits"] - 1, len(levels) - 1)]
    assert cd["retry_after"] >= first


def test_model_not_found_removal(env, keyless):
    cfg, reg, quota = env
    keyless(cfg, "amd-radeon")
    reg.add("ghost", "amd-radeon")
    class Bad:
        async def chat(self, body, model):
            raise ProviderError("404 not found", status_code=404)
    provs = {"amd-radeon": Bad()}
    r = Router(cfg, reg, quota, provs)
    with pytest.raises(AllExhausted):
        asyncio.run(r.route({"messages": [{"role": "user", "content": "x"}]}))
    assert "ghost" in reg.get_removed_ids()


def test_sticky_session(env, keyless):
    cfg, reg, quota = env
    keyless(cfg, "amd-radeon")
    r = Router(cfg, reg, quota, {"amd-radeon": MockProv("amd")})
    asyncio.run(r.route({"messages": [{"role": "user", "content": "1"}]}))
    sticky = r.session.get_sticky()
    assert sticky is not None, "会话应保持粘性"