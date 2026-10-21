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
import time
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
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
from .core.notify import Notifier
from .core import metrics, overlay as overlay_mod
from .core.observability import setup_observability, cache_span, RequestIDMiddleware
from .core.circuit_breaker import get_circuit_breaker_manager, CircuitBreakerConfig
from .core.health_check import get_health_check_manager, HealthCheckConfig
from .core.request_queue import get_request_queue, QueueConfig
from .providers import get_provider_pool
from .plugins import load_plugins
from .core.auth import AuthEngine, AuthMiddleware, set_engine, get_engine, extract_token
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
    "notifier": None,
}


def _wire_notifier(lifecycle) -> None:
    """把 Notifier 接到生命周期管理器上（重建运行期后需重新接）。"""
    nf = app_state.get("notifier")
    if nf is not None and lifecycle is not None:
        lifecycle.on_event = nf.add


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

    notifier = Notifier(Path(cfg.quota.db_path).parent / "notifications.json")

    app_state.update(config=cfg, cfg_path=cfg_path, registry=registry,
                     quota=quota, router=router, providers=providers,
                     lifecycle=lifecycle, cache=cache, probe={}, notifier=notifier)
    _wire_notifier(lifecycle)

    # 插件加载（P9）：失败只告警，不中断启动
    app_state["plugins"] = load_plugins(cfg)

    # 鉴权引擎（P9）：API Key / JWT / RBAC
    set_engine(AuthEngine(cfg))
    app_state["auth_engine"] = get_engine()

    # 历史数据升级：补齐已入库模型的能力标签（不影响发现流程）
    try:
        lifecycle.backfill_metadata()
    except Exception as e:
        logger.warning("元数据回填失败: %s", e)

    # 启动时立即做一次模型发现（后台）
    asyncio.create_task(lifecycle.run_discovery_once())
    # 用量写穿：后台定时 checkpoint（崩溃最多丢 ~10s 用量）
    checkpoint_task = asyncio.create_task(quota.checkpoint_loop())

    async def _recovery_loop():
        """内部守护：冷却一到期立即让模型回到候选；冷却过多时自动重新发现。

        取代早期外挂的 watcher.py —— 逻辑全部内建，不需要额外进程。
        """
        interval = max(5, int(getattr(cfg.quota, "recovery_check_seconds", 15)))
        while True:
            await asyncio.sleep(interval)
            try:
                q = app_state.get("quota")
                reg = app_state.get("registry")
                if q is None or reg is None:
                    continue
                freed = q.purge_expired()          # 到期的冷却立即释放
                active = reg.list_active()
                if not active:
                    continue
                cooled = q.cooled_count(active)
                if freed:
                    logger.info("冷却巡检：释放 %d 个已恢复模型（当前冷却 %d/%d）",
                                freed, cooled, len(active))
                trigger = float(getattr(cfg.quota, "cooled_ratio_trigger", 0.6))
                if cooled / len(active) >= trigger:
                    logger.warning("冷却比例过高 (%d/%d)，触发模型重新发现",
                                   cooled, len(active))
                    asyncio.create_task(lifecycle.run_discovery_once())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("恢复巡检异常: %s", e)

    recovery_task = asyncio.create_task(_recovery_loop())

    async def _cache_purge_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                cache.purge_expired()
            except Exception as e:
                logger.warning("缓存清理失败: %s", e)

    cache_task = asyncio.create_task(_cache_purge_loop())

    logger.info("Free-Router 已启动，监听 %s:%s", cfg.server_host, cfg.server_port)
    # ===== Observability Setup =====
    # Setup structured logging, tracing, metrics
    observability_cfg = cfg.observability
    setup_observability(
        app,
        config={
            "log_level": observability_cfg.log_level,
            "json_logs": observability_cfg.json_logs,
            "service_name": observability_cfg.service_name,
            "otlp_endpoint": observability_cfg.otlp_endpoint,
            "console_export": observability_cfg.console_export,
            "otlp_headers": observability_cfg.otlp_headers,
            "sampling_ratio": observability_cfg.sampling_ratio,
            "propagators": observability_cfg.propagators,
            "metric_export_interval": observability_cfg.metric_export_interval,
            "logging_file": getattr(cfg, "logging_file", None),
        }
    )
    
    # Initialize global singletons for new components
    app_state["circuit_breaker"] = get_circuit_breaker_manager(
        CircuitBreakerConfig(
            failure_threshold=observability_cfg.circuit_breaker_failure_threshold,
            success_threshold=observability_cfg.circuit_breaker_success_threshold,
            window_size=observability_cfg.circuit_breaker_window_size,
            window_time=observability_cfg.circuit_breaker_window_time,
        )
    )
    app_state["health_check"] = get_health_check_manager(
        HealthCheckConfig(
            interval_seconds=observability_cfg.health_check_interval_seconds,
            timeout_seconds=observability_cfg.health_check_timeout_seconds,
        ),
        providers=providers,
        registry=registry,
    )
    app_state["request_queue"] = get_request_queue(
        QueueConfig(
            max_size=observability_cfg.queue_max_size,
            max_wait_time=observability_cfg.queue_max_wait_time,
        )
    )
    
    # Start background tasks
    app_state["health_check_task"] = asyncio.create_task(app_state["health_check"].start())
    logger.info("observability_components_initialized")
    
    yield
    # ===== Cleanup =====
    # 关闭：停止后台任务并持久化用量
    checkpoint_task.cancel()
    cache_task.cancel()
    recovery_task.cancel()
    if app_state.get("health_check_task"):
        app_state["health_check_task"].cancel()
        try:
            await app_state["health_check_task"]
        except asyncio.CancelledError:
            pass
    quota.persist()
    logger.info("Free-Router 已关闭")


class _PluginMiddleware:
    """纯 ASGI 插件中间件：on_request / on_error（不缓冲响应，不破坏流式）。

    on_response 在端点层触发（那里才有 dict 响应体可供变换）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        plugins = app_state.get("plugins") or []
        if not plugins:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        context = {"method": scope.get("method"),
                   "path": scope.get("path"),
                   "started": time.time()}
        scope.setdefault("state", {})["_plugin_context"] = context
        for p in plugins:
            try:
                await p.on_request(request, context)
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.app(scope, receive, send)
        except Exception as e:  # noqa: BLE001
            for p in plugins:
                try:
                    await p.on_error(e, context)
                except Exception:  # noqa: BLE001
                    pass
            raise


app = FastAPI(title="Free-Router", lifespan=lifespan)
app.add_middleware(_PluginMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestIDMiddleware)

# Web 管理面板（静态单页）：http://127.0.0.1:8787/panel/
_STATIC_DIR = Path(__file__).resolve().parent / "admin_ui"
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


# ---------- 单模型别名（智能体侧只看到一个模型，后端自动轮转） ----------
_ALIAS_RESERVED = {"free-router", "freerouter", "free_router", "auto",
                   "router", "default", "auto-router", "autorouter"}


def _alias_set() -> set:
    cfg = app_state.get("config")
    alias = (getattr(cfg, "model_alias", None) or "free-router").strip().lower()
    return _ALIAS_RESERVED | {alias}


def _normalize_model_alias(body: dict) -> None:
    """把「别名 / 空模型」的请求归一为自动路由（移除 model，交给 Router 选择）。

    这样智能体侧只需要填 `free-router` 一个模型名，具体用哪个模型由本服务决定；
    显式指定真实模型名（如 kilogw/openai/gpt-5）仍然保留越权指定的能力。
    """
    if not isinstance(body, dict):
        return
    m = body.get("model")
    if m is None:
        return
    if not isinstance(m, str) or not m.strip():
        body.pop("model", None)
        return
    if m.strip().lower() in _alias_set():
        body.pop("model", None)


def _attach_meta(payload: dict, meta: dict) -> dict:
    """在响应体里也带上元信息（有些客户端读不到自定义响应头）。"""
    if isinstance(payload, dict) and meta:
        payload.setdefault("_free_router", {}).update(meta)
    return payload


# ---------- 网关访问密钥（可选）与插件响应钩子 ----------
def _access_key() -> str:
    cfg = app_state.get("config")
    return getattr(cfg, "access_key", "") or ""


def _authorized_bearer(auth_header: str) -> bool:
    """若配置了 server.access_key，则校验 Authorization: Bearer <key>；否则放行。"""
    key = _access_key()
    if not key:
        return True
    if not auth_header or not str(auth_header).lower().startswith("bearer "):
        return False
    return str(auth_header)[7:].strip() == key


def _ws_authorized(ws) -> bool:
    """WebSocket 鉴权：优先走 AuthEngine（ws scope），否则回退 server.access_key。"""
    engine = get_engine()
    if engine is not None and engine.enabled:
        headers = {k.lower(): v for k, v in (ws.headers or {}).items()}
        token = extract_token(headers)
        entry = engine.resolve(token) if token else None
        return bool(entry) and engine.authorize(entry, "/v1/ws/chat", "GET")
    return _authorized_bearer((ws.headers or {}).get("authorization", ""))


async def _run_response_plugins(response, context: dict) -> None:
    """调用所有插件的 on_response（异常单点隔离，不影响主流程）。"""
    for p in (app_state.get("plugins") or []):
        try:
            await p.on_response(response, context)
        except Exception:  # noqa: BLE001
            pass


def _plugin_context(request) -> dict:
    """取中间件写入的插件上下文（跨 on_request/on_response 共享）。"""
    return getattr(request.state, "_plugin_context", None) or {}


# ---------- OpenAI 兼容端点 ----------
@app.get("/v1/models")
async def list_models():
    """OpenAI 兼容：对外只暴露一个聚合模型别名（后端自动轮转）。

    设计目的：智能体侧只看到一个 `free-router` 模型，实际调用由本服务在
    N 个模型间自动选择/容错切换；用哪些模型在管理面板配置即可。
    如需列出全部真实模型（调试用），把 config.yaml 的
    `server.expose_single_model` 设为 false。
    """
    models = app_state["registry"].list_active()
    cfg = app_state.get("config")
    if bool(getattr(cfg, "expose_single_model", True)):
        alias = (getattr(cfg, "model_alias", None) or "free-router").strip() or "free-router"
        n = len(models)
        show_count = bool(getattr(cfg, "alias_show_count", True))
        return {
            "object": "list",
            "data": [{
                "id": alias,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "free-router",
                "name": f"{alias} ({n} 个模型轮转)" if show_count else alias,
                "description": (f"自动流转聚合模型：在 {n} 个模型间自动选择与容错切换"
                                "（使用哪些模型请在管理面板 /panel/ 配置）"),
                "rotating_models": n,
            }],
        }
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

    _normalize_model_alias(body)

    router = app_state["router"]
    if router is None:
        raise HTTPException(status_code=503, detail="router not initialized")

    ctx = _extract_ctx(request, body)
    tenant = str(ctx.get("tenant") or DEFAULT_TENANT)
    cache = app_state.get("cache")
    streaming = bool(body.get("stream"))

    # ===== Circuit Breaker Check =====
    # Pre-check: if any provider/model we might use is circuit-open, skip early
    cb = app_state.get("circuit_breaker")
    if cb:
        # We can't know which model will be selected before routing, but we can
        # check if ALL providers have open circuits (in which case we should queue)
        all_open = True
        for m in router.registry.list_active():
            if await cb.can_execute(m.provider, m.id):
                all_open = False
                break
        if all_open and app_state.get("request_queue") and app_state["request_queue"].config.max_size > 0:
            # All providers circuit-open, use request queue for backpressure
            queue = app_state["request_queue"]
            async def _do_route():
                return await router.route(body, task_ctx=ctx, tenant=tenant)
            try:
                result = await queue.enqueue(_do_route)
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "service_unavailable",
                        "message": "All providers unavailable, request queued too long",
                    },
                )
            meta = getattr(result, "meta", {}) or {}
            if hasattr(result, "__aiter__"):
                return StreamingResponse(result, media_type="text/event-stream",
                                         headers=_meta_headers(meta))
            if cache and not streaming:
                cache.set(body, dict(result), tenant,
                          provider=meta.get("provider", ""), model=meta.get("model", ""))
            return JSONResponse(content=_attach_meta(dict(result), meta),
                                headers=_meta_headers(meta))

    # 缓存命中（可选，默认只作离线兜底）
    if cache and not streaming and cache.serve_hits:
        with cache_span(hit=False):
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

    # 非流式：插件响应钩子（dict 变换，如去除思考链）+ 写缓存 + 附加元信息头
    payload = dict(result)
    await _run_response_plugins(payload, _plugin_context(request))
    if cache and not streaming:
        cache.set(body, payload, tenant,
                  provider=meta.get("provider", ""), model=meta.get("model", ""))
    return JSONResponse(content=_attach_meta(payload, meta),
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
    _normalize_model_alias(openai_body)
    ctx = _extract_ctx(request, openai_body)
    tenant = str(ctx.get("tenant") or DEFAULT_TENANT)

    # ===== Circuit Breaker Check (similar to chat_completions) =====
    cb = app_state.get("circuit_breaker")
    queue = app_state.get("request_queue")
    if cb and queue and queue.config.max_size > 0:
        # Check if all providers are circuit-open
        all_open = True
        for m in router.registry.list_active():
            if await cb.can_execute(m.provider, m.id):
                all_open = False
                break
        if all_open:
            # All providers circuit-open, use request queue for backpressure
            async def _do_route():
                return await router.route(openai_body, task_ctx=ctx, tenant=tenant)
            try:
                result = await queue.enqueue(_do_route)
            except asyncio.TimeoutError:
                return JSONResponse(
                    status_code=503,
                    content=anthropic_error(
                        "All providers unavailable, request queued too long",
                        "service_unavailable",
                    ),
                )
            meta = getattr(result, "meta", {}) or {}
            if hasattr(result, "__aiter__"):
                return StreamingResponse(
                    openai_stream_to_anthropic(result, model=body.get("model")),
                    media_type="text/event-stream",
                    headers=_meta_headers(meta)
                )
            out = openai_to_anthropic(dict(result), model=body.get("model"))
            out["_free_router"] = meta
            return JSONResponse(content=out, headers=_meta_headers(meta))

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


@app.post("/v1/batch/completions")
async def batch_completions(request: Request):
    """批量推理端点（P9）：按 max_concurrent 并发复用 router.route()。

    请求体：{"requests": [{...}, ...], "max_concurrent": 10}
    返回：  {"results": [{"index": 0, "response": {...}}, {"index": 1, "error": "..."}]}
    简化：本轮只支持非流式；流式请求返回 error 提示。
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}")

    router = app_state["router"]
    if router is None:
        raise HTTPException(status_code=503, detail="router not initialized")

    requests_list = body.get("requests") or []
    if not isinstance(requests_list, list):
        raise HTTPException(status_code=422, detail="requests 需为数组")
    try:
        max_concurrent = int(body.get("max_concurrent", 10) or 10)
    except (TypeError, ValueError):
        max_concurrent = 10
    max_concurrent = max(1, min(max_concurrent, 256))

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(i: int, req_body) -> dict:
        async with sem:
            b = dict(req_body) if isinstance(req_body, dict) else {}
            _normalize_model_alias(b)
            ctx = _extract_ctx(request, b)
            tenant = str(ctx.get("tenant") or DEFAULT_TENANT)
            try:
                result = await router.route(b, task_ctx=ctx, tenant=tenant)
            except AllExhausted as e:
                return {"index": i, "error": f"all_exhausted: {e}"}
            except Exception as e:  # noqa: BLE001
                return {"index": i, "error": f"route error: {e}"}
            meta = getattr(result, "meta", {}) or {}
            if hasattr(result, "__aiter__"):
                return {"index": i, "error": "streaming not supported in batch"}
            return {"index": i, "response": _attach_meta(dict(result), meta)}

    results = await asyncio.gather(
        *[_one(i, r) for i, r in enumerate(requests_list)])
    return {"results": results}


@app.websocket("/v1/ws/chat")
async def ws_chat(ws: WebSocket):
    """WebSocket 聊天端点：连接后收发 JSON，复用 router.route（流式透传 SSE）。"""
    await _handle_ws_chat(ws)


async def _handle_ws_chat(ws: WebSocket):
    if not _ws_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    router = app_state.get("router")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                body = json.loads(raw)
            except Exception:
                await ws.send_text(json.dumps({"error": "invalid json"}))
                continue
            if not isinstance(body, dict):
                await ws.send_text(json.dumps({"error": "body must be an object"}))
                continue
            _normalize_model_alias(body)
            if router is None:
                await ws.send_text(json.dumps({"error": "router not initialized"}))
                continue
            ctx = _extract_ctx(ws, body)
            tenant = str(ctx.get("tenant") or DEFAULT_TENANT)
            try:
                result = await router.route(body, task_ctx=ctx, tenant=tenant)
            except AllExhausted as e:
                await ws.send_text(json.dumps({"error": f"all_exhausted: {e}"}))
                continue
            except Exception as e:  # noqa: BLE001
                await ws.send_text(json.dumps({"error": f"route error: {e}"}))
                continue
            meta = getattr(result, "meta", {}) or {}
            if hasattr(result, "__aiter__"):
                async for chunk in result:
                    text = (chunk.decode("utf-8", "ignore")
                            if isinstance(chunk, (bytes, bytearray)) else str(chunk))
                    await ws.send_text(text)
            else:
                await ws.send_text(json.dumps(_attach_meta(dict(result), meta),
                                              ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("ws_chat error: %s", e)
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass


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
                                             (cfg.disabled_models or set())),
                   "model_alias": getattr(cfg, "model_alias", "free-router"),
                   "expose_single_model": bool(getattr(cfg, "expose_single_model", True)),
                   "favorites": list(getattr(cfg, "favorites", []) or [])},
        "probe": app_state.get("probe", {}),
        "notifications": {
            "unread": (app_state["notifier"].unread_count()
                       if app_state.get("notifier") else 0),
        },
    }


@app.get("/admin/favorites")
async def admin_get_favorites():
    """收藏/常用模型列表（顺序即优先级）。"""
    cfg = app_state["config"]
    return {"favorites": list(getattr(cfg, "favorites", []) or [])}


@app.post("/admin/favorites")
async def admin_set_favorites(request: Request):
    """整体替换收藏列表（持久化到覆盖层，重启后保留）。"""
    data = await _admin_params(request)
    favs = data.get("favorites")
    if favs is None:
        favs = []
    if not isinstance(favs, list):
        raise HTTPException(status_code=422, detail="favorites 需为数组")
    overlay_mod.set_favorites(app_state["cfg_path"], favs)
    _reload_runtime()
    return {"ok": True, "favorites": list(getattr(app_state["config"], "favorites", []) or [])}


# ---------- 模型更新通知（免费模型新增/下线） ----------
@app.get("/admin/notifications")
async def admin_notifications(only_unread: bool = False, limit: int = 100, ids: str = ""):
    """列出模型更新事件（新增/下线），供面板提示用户判断是否保留。
    支持 ids 参数：逗号分隔的事件 ID 列表，用于获取特定通知详情。
    """
    nf = app_state.get("notifier")
    if nf is None:
        return {"unread": 0, "events": []}

    # 支持 ids 过滤
    if ids:
        id_list = [int(x.strip()) for x in ids.split(',') if x.strip()]
        events = nf.list(only_unread=only_unread, limit=limit)
        events = [e for e in events if e.get("id") in id_list]
        return {"unread": nf.unread_count(), "events": events}

    return {"unread": nf.unread_count(),
            "events": nf.list(only_unread=only_unread, limit=limit)}


@app.post("/admin/notifications/ack")
async def admin_notifications_ack(request: Request):
    """确认（已读）若干事件，或全部已读。"""
    nf = app_state.get("notifier")
    if nf is None:
        return {"ok": True, "acked": 0}
    data = await _admin_params(request)
    ack_all = str(data.get("all", "")).lower() in ("1", "true", "yes")
    ids = data.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    n = nf.ack(ids=ids, ack_all=ack_all)
    return {"ok": True, "acked": n, "unread": nf.unread_count()}


@app.post("/admin/notifications/clear")
async def admin_notifications_clear():
    nf = app_state.get("notifier")
    n = nf.clear() if nf else 0
    return {"ok": True, "cleared": n}


# ---------- 生命周期通知（新增/更新/删除，仅展示） ----------
@app.get("/admin/lifecycle/notifications")
async def admin_lifecycle_notifications(limit: int = 100):
    """获取生命周期发现产生的通知（新增/更新/删除），仅用于展示，不影响模型正常使用。"""
    nf = app_state.get("notifier")
    if nf is None:
        return {"unread": 0, "events": []}
    evs = nf.list(only_unread=False, limit=limit)
    return {"unread": nf.unread_count(), "events": evs,
            "total": len(nf.events)}


async def _probe_provider(name: str, limit: int = 3) -> dict:
    """对某提供商的若干模型发一次极小的真实请求，测连通性与延迟。"""
    providers = app_state.get("providers") or {}
    registry = app_state["registry"]
    prov = providers.get(name)
    if prov is None:
        raise HTTPException(status_code=404, detail=f"未知提供商: {name}")
    models = [m for m in registry.list_active() if m.provider == name]
    models = models[:max(1, int(limit or 3))]
    results = []
    for m in models:
        t0 = time.time()
        try:
            await prov.chat({"messages": [{"role": "user", "content": "ping"}],
                             "max_tokens": 1, "temperature": 0}, model=m.id)
            results.append({"model": m.id, "ok": True,
                            "latency_ms": round((time.time() - t0) * 1000.0, 1)})
        except Exception as e:
            results.append({"model": m.id, "ok": False,
                            "latency_ms": round((time.time() - t0) * 1000.0, 1),
                            "error": str(e)[:160]})
    oks = [r["latency_ms"] for r in results if r.get("ok")]
    entry = {
        "provider": name,
        "ok": bool(oks),
        "models": results,
        "tested": len(results),
        "ok_count": len(oks),
        "best_latency_ms": min(oks) if oks else None,
        "avg_latency_ms": round(sum(oks) / len(oks), 1) if oks else None,
        "checked_at": time.time(),
    }
    app_state.setdefault("probe", {})[name] = entry
    return entry


@app.post("/admin/providers/{provider}/probe")
async def admin_probe_provider(provider: str, request: Request):
    """单提供商连通性/延迟探测。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    limit = int((data or {}).get("limit", 3) or 3)
    return await _probe_provider(provider, limit=limit)


@app.post("/admin/probe_all")
async def admin_probe_all(request: Request):
    """并发探测全部提供商的连通性与延迟。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    limit = int((data or {}).get("limit", 1) or 1)
    names = list((app_state.get("providers") or {}).keys())

    async def _one(n):
        try:
            return n, await _probe_provider(n, limit=limit)
        except Exception as e:
            return n, {"provider": n, "ok": False, "error": str(e)[:160]}

    pairs = await asyncio.gather(*[_one(n) for n in names])
    out = {n: r for n, r in pairs}
    return {"probe": out, "count": len(out)}


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
    _wire_notifier(lifecycle)
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


@app.post("/admin/providers/{provider}/refresh")
async def admin_refresh_provider(provider: str):
    """刷新单个提供商的模型列表（触发发现并更新注册表）。"""
    lifecycle = app_state["lifecycle"]
    # 仅刷新指定提供商
    await lifecycle.run_discovery_once(force=True, provider_filter=provider)
    _reload_runtime()
    return {"ok": True, "refreshed": provider, "models": app_state["registry"].stats()}


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


# ---------- API Key 管理（每提供商多账号） ----------
def _key_env_names(pc) -> list:
    """推导某提供商使用的环境变量名列表（首个为主名）。"""
    names = [str(x).strip() for x in (pc.keys_env or []) if str(x).strip()]
    if not names and pc.key_env:
        ke = pc.key_env
        if isinstance(ke, (list, tuple)):
            names = [str(x).strip() for x in ke if str(x).strip()]
        elif isinstance(ke, str):
            names = [x.strip() for x in ke.split(",") if x.strip()]
    if not names:
        names = [pc.name.upper().replace("-", "_").replace(".", "_") + "_API_KEY"]
    return names


def _mask_key(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return v[:2] + "***"
    return v[:4] + "…" + v[-4:]


def _env_file_path() -> Path:
    env = os.environ.get("FREE_ROUTER_KEYS_ENV")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".free-router" / ".env"


def _upsert_env(path: Path, updates: dict, remove: list):
    """在 .env 中增改/删除指定键，保留其余行与注释。"""
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    handled = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            name = s.split("=", 1)[0].strip()
            if name in updates:
                out.append(f"{name}={updates[name]}")
                handled.add(name)
                continue
            if name in remove:
                handled.add(name)
                continue
        out.append(line)
    for name, val in updates.items():
        if name not in handled:
            out.append(f"{name}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


@app.get("/admin/keys")
async def admin_keys():
    """列出各提供商已配置的 API Key 数量与掩码（不回传明文）。"""
    cfg = app_state["config"]
    out = {}
    for name, pc in cfg.providers.items():
        keys = list(dict.fromkeys(k for k in pc.resolved_keys if k))
        out[name] = {
            "key_env": _key_env_names(pc),
            "count": len(keys),
            "masked": [_mask_key(k) for k in keys],
            "strategy": pc.key_strategy,
        }
    return {"providers": out, "env_file": str(_env_file_path())}


@app.post("/admin/keys")
async def admin_set_keys(request: Request):
    """增删改某提供商的 API Key 列表。

    action: add(key) | remove(index) | clear | set(keys[])
    写入 ~/.free-router/.env（env 名按 <主名>[_N]），并热重载使新 Key 立即生效。
    """
    data = await _admin_params(request)
    provider = data.get("provider")
    if not provider:
        raise HTTPException(status_code=422, detail="需要 provider")
    cfg = app_state["config"]
    pc = cfg.providers.get(provider)
    if pc is None:
        raise HTTPException(status_code=404, detail=f"未知提供商: {provider}")

    action = str(data.get("action") or "set").lower()
    names = _key_env_names(pc)
    base = names[0]
    current = list(dict.fromkeys(k for k in pc.resolved_keys if k))

    if action == "add":
        key = (data.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=422, detail="key 不能为空")
        current.append(key)
    elif action == "remove":
        try:
            idx = int(data.get("index", -1))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="index 非法")
        if idx < 0 or idx >= len(current):
            raise HTTPException(status_code=422, detail="index 越界")
        current.pop(idx)
    elif action == "clear":
        current = []
    elif action == "set":
        keys = data.get("keys") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.splitlines() if k.strip()]
        current = [str(k).strip() for k in keys if str(k).strip()]
    else:
        raise HTTPException(status_code=422, detail=f"未知 action: {action}")

    # 重新分配环境变量名（base, base_2, base_3, ...）
    new_names, updates = [], {}
    for i, k in enumerate(current):
        nm = base if i == 0 else f"{base}_{i + 1}"
        new_names.append(nm)
        updates[nm] = k
        os.environ[nm] = k
    # 清理旧生成名（仅清理 base 及其 _N 后缀，不动其它候选名）
    remove = [n for n in names
              if (n == base or n.startswith(base + "_")) and n not in updates]
    for n in remove:
        os.environ.pop(n, None)
    _upsert_env(_env_file_path(), updates, remove)

    overlay_mod.set_provider_keys(app_state["cfg_path"], provider, new_names)
    _reload_runtime()

    pc2 = app_state["config"].providers.get(provider)
    keys2 = list(dict.fromkeys(k for k in pc2.resolved_keys if k)) if pc2 else []
    return {"ok": True, "provider": provider, "count": len(keys2),
            "masked": [_mask_key(k) for k in keys2], "env_names": new_names}


# ---------- Autofree 管理端点 ----------
@app.get("/admin/autofree/ranking")
async def admin_autofree_ranking():
    """获取 Autofree 完整榜单（Top10 + Fallback）"""
    registry = app_state["registry"]
    if not hasattr(registry, "get_autofree_all_ranked"):
        return {"top": [], "fallback": [], "top_n": 10, "fallback_n": 10, "error": "autofree not initialized"}
    ranked = registry.get_autofree_all_ranked()
    top = [r for r in ranked if r.tier == "top10"]
    fallback = [r for r in ranked if r.tier == "fallback"]
    return {
        "top": [r.__dict__ for r in top],
        "fallback": [r.__dict__ for r in fallback],
        "top_n": len(top),
        "fallback_n": len(fallback),
        "last_recompute": getattr(registry, "_autofree_scheduler", None) and getattr(registry._autofree_scheduler, "_last_recompute", 0)
    }


@app.post("/admin/autofree/recompute")
async def admin_autofree_recompute(request: Request):
    """强制重算 Autofree 榜单"""
    registry = app_state["registry"]
    if not hasattr(registry, "recompute_autofree_rank"):
        raise HTTPException(status_code=503, detail="autofree not initialized")
    data = await _admin_params(request)
    force = str(data.get("force", "")).lower() in ("1", "true", "yes")
    registry.recompute_autofree_rank(force=force)
    return {"ok": True, "forced": force}


@app.post("/admin/autofree/refresh-model")
async def admin_autofree_refresh_model(request: Request):
    """刷新单个模型的运行时统计"""
    registry = app_state["registry"]
    quota = app_state["quota"]
    if not hasattr(registry, "get_autofree_rank"):
        raise HTTPException(status_code=503, detail="autofree not initialized")
    data = await _admin_params(request)
    provider = data.get("provider")
    model = data.get("model")
    if not provider or not model:
        raise HTTPException(status_code=422, detail="需要 provider 与 model")
    # 强制从 quota 重读统计
    stats = quota.get_model_stats(provider, model)
    if hasattr(registry, "_autofree_scheduler") and registry._autofree_scheduler:
        key = f"{provider}/{model}"
        if stats:
            from .core.autofree import RuntimeStats
            registry._autofree_scheduler._model_stats_cache[key] = RuntimeStats(**stats)
    return {"ok": True, "provider": provider, "model": model, "stats": stats}


@app.post("/admin/autofree/favorite")
async def admin_autofree_favorite(request: Request):
    """切换模型收藏（影响 Autofree 评分权重）"""
    data = await _admin_params(request)
    provider = data.get("provider")
    model = data.get("model")
    if not provider or not model:
        raise HTTPException(status_code=422, detail="需要 provider 与 model")
    cfg = app_state["config"]
    favs = list(getattr(cfg, "favorites", []) or [])
    key = f"{provider}/{model}"
    if key in favs:
        favs.remove(key)
        action = "removed"
    else:
        favs.insert(0, key)
        action = "added"
    overlay_mod.set_favorites(app_state["cfg_path"], favs)
    _reload_runtime()
    return {"ok": True, "action": action, "favorites": favs}


# ---------- Smart-Free 管理端点 ----------
def _get_smart_router():
    """获取 SmartRouter 实例，如果不存在则自动创建"""
    router = app_state.get("router")
    if router is None:
        raise HTTPException(status_code=503, detail="路由器未初始化")
    if not hasattr(router, "_smart_router") or router._smart_router is None:
        # 自动初始化
        from app.core.smart_router import SmartRouter
        cfg = app_state.get("config")
        smart_cfg_dict = {}
        if cfg and hasattr(cfg, "smart_free"):
            smart_cfg_dict = {
                "load_balance_strategy": getattr(cfg.smart_free, "load_balance_strategy", "weighted_round_robin"),
                "sticky_ttl_seconds": getattr(cfg.smart_free, "sticky_ttl_seconds", 300),
                "max_retries": getattr(cfg.smart_free, "max_retries", 50),
                "min_runtime_requests": getattr(cfg.smart_free, "min_runtime_requests", 5),
            }
        router._smart_router = SmartRouter(router.registry, router.quota, smart_cfg_dict)
    return router._smart_router


def _get_sustainability():
    """获取 SustainabilityManager 实例，如果不存在则自动创建"""
    router = app_state.get("router")
    if router is None:
        raise HTTPException(status_code=503, detail="路由器未初始化")
    if not hasattr(router, "_sustainability") or router._sustainability is None:
        # 自动初始化
        from app.core.sustainability import SustainabilityManager
        cfg = app_state.get("config")
        sus_cfg_dict = {}
        if cfg and hasattr(cfg, "sustainability"):
            sus_cfg_dict = {
                "enabled": getattr(cfg.sustainability, "enabled", True),
                "global_quota_threshold": getattr(cfg.sustainability, "global_quota_threshold", 0.15),
                "health_check_interval": getattr(cfg.sustainability, "health_check_interval", 60),
                "auto_failover": getattr(cfg.sustainability, "auto_failover", True),
                "learning_enabled": getattr(cfg.sustainability, "learning_enabled", True),
            }
        router._sustainability = SustainabilityManager(router.registry, router.quota, sus_cfg_dict)
    return router._sustainability


@app.get("/admin/smart-free/ranking")
async def admin_smart_free_ranking():
    """获取 Smart-Free 智能路由的完整排名（按任务类型）"""
    sr = _get_smart_router()
    from app.core.smart_router import TaskType
    return {
        "task_types": {t.value: sr.get_top_candidates(t, limit=20) for t in TaskType},
        "load_balance_strategy": sr._load_balance_strategy,
        "providers": sr.get_provider_stats(),
    }


@app.get("/admin/smart-free/task-types")
async def admin_smart_free_task_types():
    """获取所有支持的任务类型及其匹配模型"""
    sr = _get_smart_router()
    from app.core.smart_router import TaskType
    return {
        "task_types": [
            {"value": t.value, "description": t.description}
            for t in TaskType
        ]
    }


@app.post("/admin/smart-free/recompute")
async def admin_smart_free_recompute(request: Request):
    """强制重算 Smart-Free 路由匹配分"""
    sr = _get_smart_router()
    data = await _admin_params(request)
    force = str(data.get("force", "")).lower() in ("1", "true", "yes")
    sr.recompute_match_scores(force=force)
    return {"ok": True, "forced": force}


# ---------- 可持续性管理端点 ----------
@app.get("/admin/sustainability")
async def admin_sustainability():
    """获取可持续性管理器状态：全局配额、提供商健康分、告警"""
    sus = _get_sustainability()
    return {
        "global_quota_remaining": sus.get_global_quota_remaining(),
        "global_quota_total": sus.get_global_quota_total(),
        "provider_health": sus.get_all_provider_health(),
        "alerts": sus.get_alerts(),
        "config": {
            "global_quota_threshold": sus._global_quota_threshold,
            "auto_failover": sus._auto_failover,
            "learning_enabled": sus._learning_enabled,
        }
    }


@app.get("/admin/sustainability/providers")
async def admin_sustainability_providers():
    """获取所有提供商的详细健康状态"""
    sus = _get_sustainability()
    return sus.get_provider_details()


@app.post("/admin/sustainability/health-check")
async def admin_sustainability_health_check(request: Request):
    """手动触发一次健康检查"""
    sus = _get_sustainability()
    data = await _admin_params(request)
    provider = data.get("provider")
    await sus.force_health_check(provider)
    return {"ok": True, "provider": provider}


@app.post("/admin/sustainability/reset")
async def admin_sustainability_reset(request: Request):
    """重置可持续性管理器状态（健康分、学习数据）"""
    sus = _get_sustainability()
    sus.reset()
    return {"ok": True, "message": "可持续性管理器已重置"}


# ---------- 可观测性管理端点 ----------
@app.get("/admin/circuit-breakers")
async def admin_circuit_breakers():
    """获取所有熔断器状态。"""
    cb = app_state.get("circuit_breaker")
    if not cb:
        return {"enabled": False, "breakers": {}}
    states = await cb.get_all_states()
    return {"enabled": True, "breakers": states}


@app.get("/admin/circuit-breakers/{provider}/{model}")
async def admin_circuit_breaker_detail(provider: str, model: str):
    """获取单个熔断器详细状态。"""
    cb = app_state.get("circuit_breaker")
    if not cb:
        raise HTTPException(status_code=404, detail="熔断器未启用")
    state = await cb.get_state(provider, model)
    return state


@app.post("/admin/circuit-breakers/reset")
async def admin_circuit_breakers_reset(request: Request):
    """重置熔断器（全部或指定）。"""
    from .core.circuit_breaker import reset_circuit_breakers
    data = await _admin_params(request)
    provider = data.get("provider")
    model = data.get("model")
    if provider and model:
        # Reset specific breaker (would need implementation)
        pass
    await reset_circuit_breakers()
    return {"ok": True, "message": "所有熔断器已重置"}


@app.get("/admin/health-check")
async def admin_health_check(provider: str = None, force: bool = False):
    """获取健康检查状态，可选强制立即检查。"""
    hc = app_state.get("health_check")
    if not hc:
        raise HTTPException(status_code=404, detail="健康检查未启用")
    if force:
        return await hc.force_check(provider)
    return hc.get_state(provider)


@app.get("/admin/queue")
async def admin_queue_status():
    """获取请求队列状态。"""
    queue = app_state.get("request_queue")
    if not queue:
        raise HTTPException(status_code=404, detail="请求队列未启用")
    return {
        "enabled": True,
        "size": queue.size,
        "is_full": queue.is_full,
        "is_empty": queue.is_empty,
        "max_size": queue.config.max_size,
        "max_wait_time": queue.config.max_wait_time,
    }


@app.post("/admin/queue/clear")
async def admin_queue_clear():
    """清空请求队列（慎用）。"""
    queue = app_state.get("request_queue")
    if not queue:
        raise HTTPException(status_code=404, detail="请求队列未启用")
    # Note: asyncio.Queue doesn't have a clear method, but we can recreate
    old_maxsize = queue._queue.maxsize
    queue._queue = asyncio.Queue(maxsize=old_maxsize)
    return {"ok": True, "message": "队列已清空"}