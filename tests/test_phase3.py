# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Phase 3 测试：任务感知选型 / 免费-收费提醒 / 多租户 / 额度预测 / 动态增删 /
缓存容灾 / 指标 / 可插拔策略（全部无真实网络）。"""
import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.core.config import load_config
from app.core.registry import Registry, ModelEntry
from app.core.quota import QuotaEngine
from app.core.router import Router, AllExhausted, ProviderError
from app.core.selector import ModelSelector, parse_task_context
from app.core.cache import ResponseCache
from app.core import metrics, overlay as overlay_mod
from app.core.policy import BuiltinPolicy, load_policy, make_policy

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


def _cfg(tmp_path=None):
    """用仓库配置；若给了 tmp 目录则复制一份（避免覆盖层写回仓库）。"""
    if tmp_path is None:
        return load_config(CFG_PATH, load_env_files=False)
    dst = tmp_path / "config.yaml"
    shutil.copy2(CFG_PATH, dst)
    return load_config(dst, load_env_files=False)


class OK:
    def __init__(self, name):
        self.name = name
        self.calls = 0
        self.models = []

    async def chat(self, body, model):
        self.calls += 1
        self.models.append(model)
        return {"id": model, "choices": [{"message": {"content": self.name}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _reg(cfg, tmp_path, entries):
    reg = Registry(cfg, db_path=tmp_path / "t.db")
    reg._models = {}
    for e in entries:
        reg._models[e.id] = e
    return reg


# ============================================================
# P0 任务感知选型
# ============================================================
def test_selector_free_preferred_by_default(tmp_path):
    cfg = _cfg()
    sel = ModelSelector(cfg)
    free = ModelEntry(id="f", provider="p1", tier="high", capability={"score": 60}, type="free")
    paid = ModelEntry(id="p", provider="p2", tier="high", capability={"score": 90}, type="paid")
    ordered, notice = sel.order([(free, 260.0), (paid, 290.0)], {})
    assert ordered[0].id == "f", "默认应优先免费模型"
    assert notice is None


def test_selector_paid_fallback_emits_notice(tmp_path):
    cfg = _cfg()
    sel = ModelSelector(cfg)
    paid = ModelEntry(id="p", provider="p2", tier="high", capability={"score": 90}, type="paid")
    ordered, notice = sel.order([(paid, 290.0)], {})
    assert ordered[0].id == "p"
    assert notice and "PAID" in notice, "收费模型兜底必须给出明确提醒"


def test_selector_task_tags_drive_choice(tmp_path):
    cfg = _cfg()
    sel = ModelSelector(cfg)
    vision = ModelEntry(id="v", provider="p1", tier="medium",
                        capability={"score": 50, "vision": True}, tags=["vision"])
    text = ModelEntry(id="t", provider="p2", tier="medium",
                      capability={"score": 55}, tags=["chat"])
    ctx = {"task_type": "image_description", "required_tags": ["vision"]}
    ordered, _ = sel.order([(text, 155.0), (vision, 150.0)], ctx)
    assert ordered[0].id == "v", "图像任务应选出视觉模型（即使基础分略低）"


def test_declared_vision_is_hard_requirement(tmp_path):
    """声明 required_tags=[vision] / 图像任务时，非视觉模型即使分更高也应被排除。"""
    cfg = _cfg()
    sel = ModelSelector(cfg)
    strong_text = ModelEntry(id="big", provider="p1", tier="high",
                             capability={"score": 90, "vision": False}, tags=["reasoning"])
    weak_vision = ModelEntry(id="vlm", provider="p2", tier="low",
                             capability={"score": 40, "vision": True}, tags=["vision"])
    ordered, _ = sel.order([(strong_text, 290.0), (weak_vision, 40.0)],
                           {"task_type": "image_description"})
    assert ordered == [weak_vision] or ordered[0].id == "vlm", "视觉是硬要求，不能被高分文本模型盖过"
    # 无声明时不受影响
    ordered2, _ = sel.order([(strong_text, 290.0), (weak_vision, 40.0)], {})
    assert ordered2[0].id == "big"


def test_hard_requirement_unmet_is_reported(tmp_path):
    """声明的硬能力全部不可用时：仍不中断任务，但必须如实提示。"""
    cfg = _cfg()
    sel = ModelSelector(cfg)
    text = ModelEntry(id="t", provider="p", tier="high",
                      capability={"score": 90, "vision": False})
    ordered, notice = sel.order([(text, 290.0)], {"required_tags": ["vision"]})
    assert ordered and ordered[0].id == "t"
    assert notice and "required capabilities" in notice, "能力不满足必须提示"


def test_notice_reflects_actually_used_model(tmp_path):
    """顺延/粘性后，提醒文案必须与实际命中的模型一致。"""
    cfg = _cfg()
    sel = ModelSelector(cfg)
    free = ModelEntry(id="f", provider="p", tier="high", capability={"score": 10}, type="free")
    assert sel.notice_for(free, {"prefer_free": False}) and "is FREE" in         sel.notice_for(free, {"prefer_free": False})
    paid = ModelEntry(id="p", provider="q", tier="high", capability={"score": 10}, type="paid")
    n = sel.notice_for(paid, {"prefer_free": True})
    assert n and "PAID" in n and "q/p" in n
    assert sel.notice_for(free, {}) is None


def test_selector_no_ctx_keeps_legacy_ranking(tmp_path):
    """无任务上下文时不得改变历史排序（tier 权重主导）。"""
    cfg = _cfg()
    sel = ModelSelector(cfg)
    big = ModelEntry(id="big", provider="p", tier="high", capability={"score": 70})
    small = ModelEntry(id="small", provider="q", tier="low", capability={"score": 75})
    ordered, notice = sel.order([(small, 75.0), (big, 270.0)], {})
    assert ordered[0].id == "big" and notice is None


def test_router_meta_and_headers(tmp_path):
    cfg = _cfg()
    reg = _reg(cfg, tmp_path, [ModelEntry(id="m1", provider="zhipu", tier="high",
                                          capability={"score": 70}, type="paid")])
    cfg.providers["zhipu"].key_env = ""
    cfg.providers["zhipu"].api_key = "k"
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    prov = OK("zhipu")
    r = Router(cfg, reg, quota, {"zhipu": prov})
    res = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    assert res["choices"][0]["message"]["content"] == "zhipu"
    assert res.meta["type"] == "paid"
    assert res.meta["notice"] and "PAID" in res.meta["notice"]
    assert res.meta["tenant"] == "default"


def test_router_explicit_model_wins_over_free_filter(tmp_path):
    cfg = _cfg()
    reg = _reg(cfg, tmp_path, [
        ModelEntry(id="free-m", provider="zhipu", tier="high",
                   capability={"score": 80}, type="free"),
        ModelEntry(id="paid-m", provider="dashscope", tier="high",
                   capability={"score": 80}, type="paid"),
    ])
    for p in ("zhipu", "dashscope"):
        cfg.providers[p].key_env = ""
        cfg.providers[p].api_key = "k"
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    provs = {"zhipu": OK("zhipu"), "dashscope": OK("dashscope")}
    r = Router(cfg, reg, quota, provs)
    res = asyncio.run(r.route({"model": "paid-m",
                               "messages": [{"role": "user", "content": "hi"}]}))
    assert res.meta["model"] == "paid-m", "显式指定收费模型必须生效（手动切换路径）"
    assert provs["zhipu"].calls == 0


def test_parse_task_context_sources():
    body = {"user": json.dumps({"task_type": "code_generation", "prefer_free": False})}
    assert parse_task_context(body)["task_type"] == "code_generation"
    body2 = {"free_router": {"tenant": "team-a"}}
    assert parse_task_context(body2)["tenant"] == "team-a"
    body3 = {"user": "not-json"}
    assert parse_task_context(body3) == {}


def test_sticky_does_not_bypass_hard_capability(tmp_path):
    """会话粘性不得绕过声明的硬能力（vision）。"""
    cfg = _cfg()
    reg = _reg(cfg, tmp_path, [
        ModelEntry(id="text-only", provider="zhipu", tier="high",
                   capability={"score": 90, "vision": False}),
        ModelEntry(id="vlm", provider="dashscope", tier="medium",
                   capability={"score": 50, "vision": True}),
    ])
    for p in ("zhipu", "dashscope"):
        cfg.providers[p].key_env = ""
        cfg.providers[p].api_key = "k"
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    provs = {"zhipu": OK("zhipu"), "dashscope": OK("dashscope")}
    r = Router(cfg, reg, quota, provs)
    # 先建立粘性（普通请求 → 高分文本模型）
    r1 = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    assert r1.meta["model"] == "text-only"
    # 再来一个声明 vision 的请求：粘性不得把文本模型拉回来
    r2 = asyncio.run(r.route({"messages": [{"role": "user", "content": "see"}],
                              "free_router": {"task_type": "image_description"}}))
    assert r2.meta["model"] == "vlm", "粘性不能绕过 vision 硬要求"


def test_registry_unremove_allows_readd(tmp_path):
    cfg = _cfg()
    reg = Registry(cfg, db_path=tmp_path / "t.db")
    reg.add("m-x", "kilogw")
    reg.remove("m-x")
    assert reg.add("m-x", "kilogw") is False, "墓碑期不得复活（自动发现保护）"
    reg.unremove("m-x")
    assert reg.add("m-x", "kilogw") is True, "显式重新加入应生效"


# ============================================================
# P4 多租户
# ============================================================
def test_tenant_isolation(tmp_path):
    cfg = _cfg()
    q = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    for _ in range(3):
        q.record("kilo", "m", tenant="team-a")
    q.record("kilo", "m", tenant="team-b")
    snap = q.snapshot()
    assert snap["usage"]["team-a:kilo/m"]["rpd"] == 3
    assert snap["usage"]["team-b:kilo/m"]["rpd"] == 1
    # 冷却也按租户隔离
    q.apply_cooldown("kilo", "m", "429", tenant="team-a")
    assert q.in_cooldown("kilo", "m", tenant="team-a") is True
    assert q.in_cooldown("kilo", "m", tenant="team-b") is False


def test_default_tenant_label_backward_compatible(tmp_path):
    cfg = _cfg()
    q = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    q.record("kilo", "m")
    assert "kilo/m" in q.snapshot()["usage"], "默认租户标签须与历史一致（不带前缀）"


def test_legacy_db_migration(tmp_path):
    """旧库（usage/cooldown 无 tenant 列）自动迁移到 default 租户。"""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage(provider TEXT, model TEXT, key_hash TEXT,
        rpm_cnt INTEGER, rpm_window REAL, rpd_cnt INTEGER, rpd_day TEXT,
        tpm_tokens INTEGER, tpm_window REAL, tpd_tokens INTEGER, tpd_day TEXT,
        PRIMARY KEY(provider, model, key_hash))""")
    con.execute("""CREATE TABLE cooldown(provider TEXT, model TEXT, key_hash TEXT,
        reason TEXT, until REAL, retry_after INTEGER, hits INTEGER,
        PRIMARY KEY(provider, model, key_hash))""")
    con.execute("INSERT INTO usage VALUES('kilo','m','',1,0,5,'2026-01-01',0,0,0,'')")
    con.commit(); con.close()

    cfg = _cfg()
    q = QuotaEngine(cfg, db_path=db)
    assert q.snapshot()["usage"].get("kilo/m", {}).get("rpd") == 5, "旧数据应迁入 default"
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(usage)")]
    assert "tenant" in cols


# ============================================================
# P3 额度预测
# ============================================================
def test_prediction_flags_soon_exhausted(tmp_path):
    cfg = _cfg()
    cfg.quota.rpd_limit = 1000
    cfg.quota.predict_window_seconds = 30
    cfg.quota.predict_threshold_seconds = 20
    q = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    assert q.predict_exhausted("kilo", "m") is False
    for _ in range(700):          # 速率 ~23/s，剩余 300 → 约 13s 耗尽
        q.record("kilo", "m")
    assert q.predict_exhausted("kilo", "m") is True
    assert "kilo/m" in q.prediction_snapshot()


def test_prediction_filters_candidate_in_router(tmp_path):
    cfg = _cfg()
    cfg.quota.rpd_limit = 1000
    reg = _reg(cfg, tmp_path, [
        ModelEntry(id="fast", provider="zhipu", tier="high", capability={"score": 80}),
        ModelEntry(id="idle", provider="dashscope", tier="medium", capability={"score": 60}),
    ])
    for p in ("zhipu", "dashscope"):
        cfg.providers[p].key_env = ""
        cfg.providers[p].api_key = "k"
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    for _ in range(700):
        quota.record("zhipu", "fast")
    r = Router(cfg, reg, quota, {"zhipu": OK("zhipu"), "dashscope": OK("dashscope")})
    res = asyncio.run(r.route({"messages": [{"role": "user", "content": "hi"}]}))
    assert res.meta["model"] == "idle", "预计即将耗尽的模型应被提前避开"


# ============================================================
# P1 动态增删（覆盖层）
# ============================================================
def test_overlay_add_and_remove_model(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    shutil.copy2(CFG_PATH, cfg_path)

    overlay_mod.add_model(cfg_path, "kilogw", "vendor/new-model:free",
                          type="free", tags=["reasoning"], tier="high", priority=2)
    cfg2 = load_config(cfg_path, load_env_files=False)
    ids = [m.get("id") for m in cfg2.providers["kilogw"].models]
    assert "vendor/new-model:free" in ids, "覆盖层新增模型应出现在配置里"
    reg = Registry(cfg2, db_path=tmp_path / "t.db")
    m = reg.get("vendor/new-model:free")
    assert m and m.type == "free" and "reasoning" in m.tags and m.priority == 2

    overlay_mod.remove_model(cfg_path, "kilogw", "vendor/new-model:free")
    cfg3 = load_config(cfg_path, load_env_files=False)
    ids3 = [m.get("id") for m in cfg3.providers["kilogw"].models]
    assert "vendor/new-model:free" not in ids3
    assert ("kilogw", "vendor/new-model:free") in cfg3.disabled_models
    reg3 = Registry(cfg3, db_path=tmp_path / "t3.db")
    assert reg3.get("vendor/new-model:free") is None, "墓碑模型不得再入池"


def test_overlay_add_provider(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    shutil.copy2(CFG_PATH, cfg_path)
    overlay_mod.add_provider(cfg_path, "my-gw", "https://example.com/v1",
                             type="free", key_env="MY_GW_KEY")
    overlay_mod.add_model(cfg_path, "my-gw", "some-model:free", tags=["chat"])
    cfg = load_config(cfg_path, load_env_files=False)
    assert "my-gw" in cfg.providers
    assert cfg.providers["my-gw"].base_url == "https://example.com/v1"
    assert cfg.providers["my-gw"].models[0]["id"] == "some-model:free"
    # api_key 必须留空（用户要求：密钥一律不落配置）
    assert (cfg.providers["my-gw"].api_key or "") == ""


def test_registry_add_respects_disabled(tmp_path):
    cfg = _cfg()
    cfg.disabled_models = {("kilogw", "blocked-model")}
    reg = Registry(cfg, db_path=tmp_path / "t.db")
    assert reg.add("blocked-model", "kilogw") is False
    assert reg.get("blocked-model") is None


# ============================================================
# P2 缓存容灾
# ============================================================
def test_cache_stale_fallback(tmp_path):
    cache = ResponseCache(tmp_path / "c.db", enabled=True, ttl_seconds=0,
                          serve_hits=False)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    cache.set(body, {"choices": [{"message": {"content": "cached"}}]},
              provider="p", model="m")
    assert cache.get(body) is None, "ttl=0 时不应命中新鲜缓存"
    stale = cache.get(body, allow_stale=True)
    assert stale and stale["choices"][0]["message"]["content"] == "cached"
    assert stale["_free_router"]["cache"] == "STALE-FALLBACK"
    assert cache.get({"messages": [{"role": "user", "content": "other"}]},
                     allow_stale=True) is None, "不同请求不得串缓存"


def test_cache_ignores_error_payload(tmp_path):
    cache = ResponseCache(tmp_path / "c2.db", ttl_seconds=60)
    body = {"messages": []}
    cache.set(body, {"error": "boom"})
    assert cache.get(body) is None, "错误响应不得进缓存"


# ============================================================
# P5 指标
# ============================================================
def test_metrics_render():
    metrics.count_request("kilo", "m1", "success")
    metrics.count_fallback("amd-radeon", "kilogw")
    metrics.count_cache("hit")
    text = metrics.render_prometheus()
    assert 'free_router_requests_total{provider="kilo",model="m1",status="success"} 1' in text
    assert "free_router_fallback_switches_total" in text
    assert 'free_router_cache_events_total{kind="hit"} 1' in text
    assert "# TYPE free_router_uptime_seconds gauge" in text


# ============================================================
# P7 可插拔策略
# ============================================================
def test_default_policy_is_builtin(tmp_path):
    cfg = _cfg()
    assert isinstance(load_policy(cfg), BuiltinPolicy)


def test_custom_policy_via_config(tmp_path, monkeypatch):
    """配置里的 policy 模块被动态加载；坏配置回退内置（不影响可用性）。"""
    cfg = _cfg()
    cfg.router.policy = {"module": "nosuch.module", "class": "Nope"}
    assert isinstance(load_policy(cfg), BuiltinPolicy)

    import types
    mod = types.ModuleType("fake_policy_mod")

    class AlwaysPaid:
        def score(self, entry, ctx, base):
            return base + (1000.0 if entry.type == "paid" else 0.0)

    mod.AlwaysPaid = AlwaysPaid
    monkeypatch.setitem(sys.modules, "fake_policy_mod", mod)
    obj = make_policy("fake_policy_mod", "AlwaysPaid")
    assert isinstance(obj, AlwaysPaid)

    # 策略生效：免费偏好下，策略把收费模型顶到最前
    sel = ModelSelector(cfg, policy=obj)
    free = ModelEntry(id="f", provider="p", tier="high", capability={"score": 10}, type="free")
    paid = ModelEntry(id="p", provider="q", tier="high", capability={"score": 10}, type="paid")
    ordered, _ = sel.order([(free, 210.0), (paid, 210.0)], {"prefer_free": False})
    assert ordered[0].id == "p"


def test_policy_exception_falls_back(tmp_path):
    cfg = _cfg()

    class Boom:
        def score(self, entry, ctx, base):
            raise RuntimeError("policy broken")

    sel = ModelSelector(cfg, policy=Boom())
    e = ModelEntry(id="m", provider="p", tier="high", capability={"score": 50})
    ordered, _ = sel.order([(e, 250.0)], {"task_type": "chat"})
    assert ordered[0].id == "m", "策略异常不得中断路由"


# ============================================================
# 管理端点（动态增删落到运行期）
# ============================================================
def test_admin_add_and_delete_model_via_app(tmp_path):
    from app import main as app_main
    cfg_path = tmp_path / "config.yaml"
    shutil.copy2(CFG_PATH, cfg_path)
    cfg = load_config(cfg_path, load_env_files=False)
    reg = Registry(cfg, db_path=tmp_path / "t.db")
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    from app.core.lifecycle import LifecycleManager
    from app.providers import get_provider_pool
    providers = get_provider_pool(cfg)

    class _FakeReq:
        def __init__(self, body):
            self._body = body
            self.query_params = {}
            self.headers = {}

        async def json(self):
            return self._body

    old = dict(app_main.app_state)
    try:
        app_main.app_state.update(config=cfg, cfg_path=cfg_path, registry=reg,
                                  quota=quota, providers=providers,
                                  lifecycle=LifecycleManager(cfg, reg, quota, providers),
                                  router=Router(cfg, reg, quota, providers),
                                  cache=ResponseCache(tmp_path / "c.db"))
        asyncio.run(app_main.admin_add_model(_FakeReq(
            {"provider": "kilogw", "model": "x/y-model:free",
             "type": "free", "tags": "chat,reasoning", "tier": "high"})))
        assert reg.get("x/y-model:free") is not None
        st = asyncio.run(app_main.admin_models())
        assert any(m["model"] == "x/y-model:free" for m in st["models"])

        asyncio.run(app_main.admin_del_model(_FakeReq(
            {"provider": "kilogw", "model": "x/y-model:free"})))
        assert reg.get("x/y-model:free") is None or \
            reg.get("x/y-model:free").status == "removed"
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)


def test_admin_status_shape(tmp_path):
    from app import main as app_main
    cfg = _cfg()
    reg = _reg(cfg, tmp_path, [ModelEntry(id="m", provider="kilogw", tier="high")])
    quota = QuotaEngine(cfg, db_path=tmp_path / "t.db")
    from app.core.lifecycle import LifecycleManager
    old = dict(app_main.app_state)
    try:
        app_main.app_state.update(config=cfg, cfg_path=CFG_PATH, registry=reg,
                                  quota=quota, providers={},
                                  lifecycle=LifecycleManager(cfg, reg, quota, {}),
                                  router=Router(cfg, reg, quota, {}),
                                  cache=ResponseCache(tmp_path / "c.db"))
        st = asyncio.run(app_main.admin_status())
        for key in ("models", "quota", "cooldowns", "predicted_exhausted",
                    "cache", "metrics", "alerter", "policy", "config"):
            assert key in st, f"admin/status 缺少 {key}"
        assert "by_type" in st["models"]
    finally:
        app_main.app_state.clear()
        app_main.app_state.update(old)