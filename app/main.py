# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Free-Router 主应用入口 (FastAPI)
OpenAI 兼容端点，聚合多家免费/收费提供商，自动路由 + 切换。

无框架依赖：任何能发 HTTP 的智能体把 base_url 指向本服务即可使用。
管理面（/admin/*）用于运行期增删提供商/模型、切换策略、查看额度与指标。
"""
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .core.config import Config, load_config
from .core.registry import Registry
from .core.router import Router
from .core.quota import QuotaEngine, DEFAULT_TENANT
from .core.lifecycle import LifecycleManager
from .core.router import AllExhausted
from .core.selector import parse_task_context
from .core.cache import ResponseCache
from .core import metrics, overlay as overlay_mod
from .providers import get_provider_pool
from .anthropic import (anthropic_to_openai, openai_to_anthropic,
                        openai_stream_to_anthropic, anthropic_error)

logger = logging.getLogger("free_router")

# 全局单例（模块级，便于 uvicorn 与测试）
app_state = {
    "config": None,
    "cfg_path": None,
    "registry": None,
    "quota": None,
    "router": None,
    "lifecycle": None,
    "providers": None,
    "cache": None,
}


def _resolve_cfg_path() -> Path:
    cfg_path = os.environ.get("FREE_ROUTER_CONFIG")
    return Path(cfg_path) if cfg_path else \
        Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def _build_runtime(cfg, registry=None, quota=None):
    """按配置构建/重建运行期组件（保留已有的 quota 用量与 registry 池）。"""
    registry = registry or Registry(cfg, db_path=Path(cfg.quota.db_path))
    quota = quota or QuotaEngine(cfg, db_path=Path(cfg.quota.db_path))
    quota.cfg = cfg
    providers = get_provider_pool(cfg)
    lifecycle = LifecycleManager(cfg, registry, quota, providers)
    router = Router(cfg, registry, quota, providers)
    return registry, quota, providers, lifecycle, router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：加载配置、初始化各组件
    cfg_path = _resolve_cfg_path()
    cfg = load_config(cfg_path)
    registry, quota, providers, lifecycle, router = _build_runtime(cfg)

    cache_cfg = cfg.cache or {}
    cache = ResponseCache(Path(cfg.quota.db_path),
                          enabled=cache_cfg.get("enabled", True),
                          ttl_seconds=cache_cfg.get("ttl_seconds", 86400),
                          max_entries=cache_cfg.get("max_entries", 2000),
                          serve_hits=cache_cfg.get("serve_hits", False))

    app_state.update(config=cfg, cfg_path=cfg_path, registry=registry,
                     quota=quota, router=router, providers=providers,
                     lifecycle=lifecycle, cache=cache)

    # 启动时立即做一次模型发现（后台）
    asyncio.create_task(lifecycle.run_discovery_once())
    # 用量写穿：后台定时 checkpoint（崩溃最多丢 ~10s 用量）
    checkpoint_task = asyncio.create_task(quota.checkpoint_loop())

    async def _cache_purge_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                cache.purge_expired()
            except Exception as e:
                logger.warning("缓存清理失败: %s", e)

    cache_task = asyncio.create_task(_cache_purge_loop())

    logger.info("Free-Router 已启动，监听 %s:%s", cfg.server_host, cfg.server_port)
    yield
    # 关闭：停止后台任务并持久化用量
    checkpoint_task.cancel()
    cache_task.cancel()
    quota.persist()
    logger.info("Free-Router 已关闭")


app = FastAPI(title="Free-Router", lifespan=lifespan)

# Web 管理面板（静态单页）：http://127.0.0.1:8787/panel/
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/panel", StaticFiles(directory=str(_STATIC_DIR), html=True), name="panel")


# ---------- 任务上下文 / 响应头 ----------
def _ascii(v) -> str:
    """HTTP 头只能是 latin-1：非 ASCII 字符替换为 '?'，避免编码异常。"""
    return str(v).encode("ascii", "replace").decode("ascii")


def _extract_ctx(request: Request, body: dict) -> dict:
    """任务上下文来源：请求体（free_router/user/metadata）+ 自定义请求头。"""
    ctx = parse_task_context(body)
    hdrs = getattr(request, "headers", None)
    if hdrs is None:                      # 测试替身可能没有 headers
        return ctx
    for hdr, key in (("x-free-router-task-type", "task_type"),
                     ("x-free-router-tenant", "tenant"),
                     ("x-free-router-model-type", "model_type"),
                     ("x-free-router-prefer-free", "prefer_free")):
        v = hdrs.get(hdr)
        if v is None:
            continue
        if key == "prefer_free":
            ctx[key] = str(v).strip().lower() not in ("0", "false", "no", "off")
        else:
            ctx[key] = v
    tags = getattr(getattr(request, "headers", None), "get", lambda *_: None)(
        "x-free-router-tags")
    if tags:
        ctx["required_tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    return ctx


def _meta_headers(meta: dict) -> dict:
    if not meta:
        return {}
    h = {
        "X-Free-Router-Provider": _ascii(meta.get("provider", "")),
        "X-Free-Router-Model": _ascii(meta.get("model", "")),
        "X-Free-Router-Model-Type": _ascii(meta.get("type", "free")),
    }
    if meta.get("notice"):
        h["X-Free-Router-Notice"] = _ascii(meta["notice"])
    if meta.get("cache"):
        h["X-Free-Router-Cache"] = _ascii(meta["cache"])
    return h


def _attach_meta(payload: dict, meta: dict) -> dict:
    """在响应体里也带上元信息（有些客户端读不到自定义响应头）。"""
    if isinstance(payload, dict) and meta:
        payload.setdefault("_free_router", {}).update(meta)
    return payload


# ---------- OpenAI 兼容端点 ----------
@app.get("/v1/models")
async def list_models():
    """OpenAI 兼容：列出可用模型（仅返回虚拟聚合模型 free-router）"""
    # 只暴露 free-router 聚合模型，隐藏所有具体的下游模型
    aggregator = app_state["registry"].get("free-router")
    if aggregator and aggregator.status == "active":
        models = [aggregator]
    else:
        # 兜底：如果聚合模型不存在，返回空列表
        models = []
    return {
        "object": "list",
        "data": [
            {"id": m.id, "object": "model",
             "owned_by": m.provider, "capability": m.capability,
             "type": getattr(m, "type", "free"), "tier": getattr(m, "tier", "medium"),
             "tags": list(getattr(m, "tags", []) or [])}
            for m in models
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容：聊天补全端点。核心路由逻辑。"""
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}")

    router = app_state["router"]
    if router is None:
        raise HTTPException(status_code=503, detail="router not initialized")

    ctx = _extract_ctx(request, body)
    tenant = str(ctx.get("tenant") or DEFAULT_TENANT)
    cache = app_state.get("cache")
    streaming = bool(body.get("stream"))

    # 缓存命中（可选，默认只作离线兜底）
    if cache and not streaming and cache.serve_hits:
        hit = cache.get(body, tenant)
        if hit:
            metrics.count_cache("hit")
            return JSONResponse(content=_attach_meta(hit, hit.get("_free_router", {})),
                                headers=_meta_headers(hit.get("_free_router", {})))

    try:
        result = await router.route(body, task_ctx=ctx, tenant=tenant)
    except AllExhausted as e:
        # 全部不可用 → 先用本地缓存的最近成功响应兜底，保证任务不中断
        if cache and not streaming:
            stale = cache.get(body, tenant, allow_stale=True)
            if stale:
                metrics.count_cache("stale")
                metrics.count_request("-", "-", "cache_stale")
                meta = stale.get("_free_router", {})
                meta["notice"] = f"all models exhausted; serving cached response ({e})"
                return JSONResponse(content=_attach_meta(stale, meta),
                                    headers=_meta_headers(meta))
        metrics.count_request("-", "-", "all_exhausted")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "all_free_models_exhausted",
                "last_error": str(e),
                "hint": "请稍后重试，或检查各提供商 API key 与额度",
            },
        )
    except Exception as e:
        logger.exception("route failed")
        raise HTTPException(status_code=500, detail=f"route error: {e}")

    meta = getattr(result, "meta", {}) or {}

    # 流式：逐块透传
    if hasattr(result, "__aiter__"):
        return StreamingResponse(result, media_type="text/event-stream",
                                 headers=_meta_headers(meta))

    # 非流式：写缓存 + 附加元信息头
    if cache and not streaming:
        cache.set(body, dict(result), tenant,
                  provider=meta.get("provider", ""), model=meta.get("model", ""))
    return JSONResponse(content=_attach_meta(dict(result), meta),
                        headers=_meta_headers(meta))


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API 兼容端点：转 OpenAI → 复用 Router → 转回。"""
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400,
                            content=anthropic_error(f"invalid json: {e}",
                                                    "invalid_request_error"))

    router = app_state["router"]
    if router is None:
        return JSONResponse(status_code=503,
                            content=anthropic_error("router not initialized",
                                                    "overloaded_error"))

    openai_body = anthropic_to_openai(body)
    ctx = _extract_ctx(request, openai_body)
    tenant = str(ctx.get("tenant") or DEFAULT_TENANT)

    try:
        result = await router.route(openai_body, task_ctx=ctx, tenant=tenant)
    except AllExhausted as e:
        cache = app_state.get("cache")
        if cache:
            stale = cache.get(openai_body, tenant, allow_stale=True)
            if stale:
                out = openai_to_anthropic(stale, model=body.get("model"))
                out["_free_router"] = stale.get("_free_router", {})
                return JSONResponse(content=out,
                                    headers=_meta_headers(out["_free_router"]))
        return JSONResponse(
            status_code=503,
            content=anthropic_error(f"all_free_models_exhausted: {e}",
                                    "overloaded_error"),
        )
    except Exception as e:
        logger.exception("anthropic route failed")
        return JSONResponse(status_code=500,
                            content=anthropic_error(f"route error: {e}"))

    meta = getattr(result, "meta", {}) or {}

    # 流式（可选）：OpenAI SSE → Anthropic SSE
    if hasattr(result, "__aiter__"):
        return StreamingResponse(
            openai_stream_to_anthropic(result, model=body.get("model")),
            media_type="text/event-stream", headers=_meta_headers(meta))
    out = openai_to_anthropic(dict(result), model=body.get("model"))
    out["_free_router"] = meta
    return JSONResponse(content=out, headers=_meta_headers(meta))


@app.get("/health")
async def health():
    st = app_state
    return {"status": "ok",
            "providers": list((st.get("providers") or {}).keys()),
            "models": (st["registry"].stats() if st.get("registry") else {})}


# ---------- 指标（P5） ----------
@app.get("/metrics")
async def prometheus_metrics():
    if not (app_state.get("config") and app_state["config"].metrics.get("enabled", True)):
        raise HTTPException(status_code=404, detail="metrics disabled")
    return PlainTextResponse(metrics.render_prometheus(),
                             media_type="text/plain; version=0.0.4")


# ---------- 管理接口 ----------
@app.get("/admin/status")
async def admin_status():
    quota = app_state["quota"]
    cfg = app_state["config"]
    return {
        "models": app_state["registry"].stats(),
        "quota": quota.snapshot(),
        "cooldowns": quota.cooldown_snapshot(),
        "predicted_exhausted": quota.prediction_snapshot(),
        "cache": app_state["cache"].stats() if app_state.get("cache") else {},
        "metrics": metrics.snapshot(),
        "alerter": app_state["router"].alerter.snapshot() if app_state.get("router") else {},
        "policy": type(app_state["router"].selector.policy).__name__
        if app_state.get("router") else None,
        "config": {"providers": list(cfg.providers.keys()),
                   "disabled_models": sorted(f"{p}/{m}" for p, m in
                                             (cfg.disabled_models or set()))},
    }


@app.get("/admin/models")
async def admin_models():
    """列出池中模型（含档位/标签/优先级），便于运维核对。"""
    out = []
    for m in app_state["registry"].list_active():
        out.append({"provider": m.provider, "model": m.id,
                    "type": getattr(m, "type", "free"),
                    "tier": getattr(m, "tier", "medium"),
                    "tags": list(getattr(m, "tags", []) or []),
                    "priority": getattr(m, "priority", 0)})
    return {"count": len(out), "models": out,
            "disabled": sorted(f"{p}/{m}" for p, m in
                               (app_state["config"].disabled_models or set()))}


@app.post("/admin/refresh")
async def admin_refresh():
    await app_state["lifecycle"].run_discovery_once(force=True)
    return {"ok": True, "models": app_state["registry"].stats()}


async def _admin_params(request: Request) -> dict:
    """从 query 参数或 JSON body 中取参（兼容 skill CLI 的 body 传参）。"""
    data = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            for k, v in body.items():
                data.setdefault(k, v)
    except Exception:
        pass
    return data


def _reload_runtime() -> None:
    """覆盖层变更后重建运行期：新提供商立即可用，池内新增模型立即入池。"""
    cfg_path = app_state["cfg_path"]
    cfg = load_config(cfg_path)
    registry, quota, providers, lifecycle, router = _build_runtime(
        cfg, registry=app_state["registry"], quota=app_state["quota"])
    app_state.update(config=cfg, registry=registry, quota=quota,
                     providers=providers, lifecycle=lifecycle, router=router)
    for pname, pc in cfg.providers.items():
        for m in pc.models:
            if m.get("id"):
                registry.unremove(m["id"])      # 覆盖层里显式存在的模型 → 清除墓碑
                registry.add(m["id"], pname,
                             capability=m.get("capability") or {},
                             tier=m.get("tier", pc.tier_quality),
                             type=m.get("type"), tags=m.get("tags"),
                             priority=m.get("priority", 0))


@app.post("/admin/models")
async def admin_add_model(request: Request):
    """动态新增模型（可同时新建提供商）。写入覆盖层，重启后仍生效。"""
    data = await _admin_params(request)
    provider, model = data.get("provider"), data.get("model")
    if not provider or not model:
        raise HTTPException(status_code=422, detail="需要 provider 与 model")
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    overlay_mod.add_model(app_state["cfg_path"], provider, model,
                          base_url=data.get("base_url"),
                          type=data.get("type"), tags=tags,
                          tier=data.get("tier"),
                          priority=int(data.get("priority", 0) or 0))
    _reload_runtime()
    return {"ok": True, "added": {"provider": provider, "model": model},
            "models": app_state["registry"].stats()}


@app.delete("/admin/models")
async def admin_del_model(request: Request):
    """动态下线模型（墓碑写进覆盖层；自动发现也不会再把它加回来）。"""
    data = await _admin_params(request)
    provider, model = data.get("provider"), data.get("model")
    if not provider or not model:
        raise HTTPException(status_code=422, detail="需要 provider 与 model")
    overlay_mod.remove_model(app_state["cfg_path"], provider, model)
    app_state["registry"].remove(model)
    _reload_runtime()
    return {"ok": True, "removed": {"provider": provider, "model": model},
            "models": app_state["registry"].stats()}


@app.post("/admin/providers")
async def admin_add_provider(request: Request):
    """动态新增提供商（openai 兼容端点）。"""
    data = await _admin_params(request)
    name, base_url = data.get("name") or data.get("provider"), data.get("base_url")
    if not name or not base_url:
        raise HTTPException(status_code=422, detail="需要 name 与 base_url")
    overlay_mod.add_provider(app_state["cfg_path"], name, base_url,
                             type=data.get("type"),
                             key_env=data.get("key_env"),
                             free_only=data.get("free_only"))
    _reload_runtime()
    return {"ok": True, "added": {"provider": name, "base_url": base_url},
            "providers": list(app_state["providers"].keys())}


@app.delete("/admin/providers")
async def admin_del_provider(request: Request):
    data = await _admin_params(request)
    name = data.get("name") or data.get("provider")
    if not name:
        raise HTTPException(status_code=422, detail="需要 name")
    overlay_mod.remove_provider(app_state["cfg_path"], name)
    _reload_runtime()
    return {"ok": True, "removed": name,
            "providers": list(app_state["providers"].keys())}


@app.post("/admin/switch")
async def admin_switch(request: Request):
    """强制切换模型。参数顺序与 skill/scripts/fr.py 一致：model, provider。

    幂等：重复调用同一 (model, provider) 结果相同。
    """
    data = await _admin_params(request)
    model = data.get("model")
    provider = data.get("provider")
    # 兼容 'provider/model' 单串写法
    if model and "/" in model and not provider:
        provider, model = model.split("/", 1)
    if not model or not provider:
        raise HTTPException(status_code=422,
                            detail="需要 model 与 provider（或 model='provider/model'）")
    app_state["router"].force_switch(provider, model)
    return {"ok": True, "switched_to": {"provider": provider, "model": model}}


@app.post("/admin/cooldown")
async def admin_cooldown(request: Request):
    data = await _admin_params(request)
    provider = data.get("provider")
    model = data.get("model")
    reason = data.get("reason", "429")
    if not provider or not model:
        raise HTTPException(status_code=422, detail="需要 provider 与 model")
    app_state["quota"].force_cooldown(provider, model, reason)
    return {"ok": True, "cooled": {"provider": provider, "model": model}}


@app.post("/admin/reset_cooldowns")
async def admin_reset_cooldowns():
    n = app_state["quota"].reset_all_cooldowns()
    return {"ok": True, "reset": n}


# ---------- 策略（P7）/ 缓存（P2） ----------
@app.get("/admin/policy")
async def admin_get_policy():
    router = app_state["router"]
    pol = router.selector.policy
    return {"policy": type(pol).__name__,
            "module": type(pol).__module__,
            "config": app_state["config"].router.policy,
            "builtin": type(pol).__name__ == "BuiltinPolicy"}


@app.post("/admin/policy")
async def admin_set_policy(request: Request):
    """运行期热加载打分策略：{module, class, args?}；传空则恢复内置。"""
    from .core.policy import load_policy, make_policy, BuiltinPolicy
    data = await _admin_params(request)
    module, cls = data.get("module"), data.get("class")
    if not module or not cls:
        app_state["router"].selector.policy = BuiltinPolicy()
        return {"ok": True, "policy": "BuiltinPolicy"}
    try:
        obj = make_policy(module, cls, data.get("args") or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"策略加载失败: {e}")
    app_state["router"].selector.policy = obj
    return {"ok": True, "policy": cls}


@app.get("/admin/cache")
async def admin_cache_stats():
    cache = app_state.get("cache")
    if cache is None:
        return {"enabled": False}
    out = cache.stats()
    out["events"] = metrics.snapshot().get("cache_events", {})
    return out


@app.post("/admin/cache")
async def admin_cache_purge(request: Request):
    data = await _admin_params(request)
    if str(data.get("action", "")).lower() in ("purge", "clear", "clean"):
        n = app_state["cache"].purge_expired()
        return {"ok": True, "purged": n}
    return {"ok": False, "hint": "action=purge"}