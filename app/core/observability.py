"""Observability: structured logging, OpenTelemetry, Prometheus metrics.

This module centralizes all observability concerns:
- structlog-based JSON logging with request_id correlation
- OpenTelemetry tracing for request flow
- Prometheus metrics (using prometheus-client)
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import structlog
from opentelemetry import metrics, propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ============================================================
# Prometheus Metrics (using prometheus-client)
# ============================================================

# Try to import prometheus_client metrics, handle Windows compatibility
PROMETHEUS_AVAILABLE = False

# Use importlib to dynamically import and catch import-time errors
try:
    import importlib
    prometheus_client = importlib.import_module('prometheus_client')
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    metrics_registry = CollectorRegistry()
    PROMETHEUS_AVAILABLE = True
except (ImportError, AttributeError, OSError, ModuleNotFoundError):
    # Windows compatibility: resource.getpagesize() not available
    PROMETHEUS_AVAILABLE = False
    # Create dummy classes
    class _DummyMetric:
        def __init__(self, *args, **kwargs): pass
        def inc(self, *args, **kwargs): pass
        def dec(self, *args, **kwargs): pass
        def set(self, *args, **kwargs): pass
        def observe(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
    
    Counter = _DummyMetric
    Gauge = _DummyMetric
    Histogram = _DummyMetric
    CollectorRegistry = lambda: None
    CONTENT_TYPE_LATEST = "text/plain"
    def generate_latest(*args, **kwargs): return b"# Prometheus not available on this platform\n"
    metrics_registry = None

# Metrics instances (will be dummy if PROMETHEUS_AVAILABLE is False)
http_requests_total = Counter(
    "free_router_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
    registry=metrics_registry,
)

http_request_duration_seconds = Histogram(
    "free_router_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=metrics_registry,
)

# Provider metrics
provider_requests_total = Counter(
    "free_router_provider_requests_total",
    "Total requests to each provider",
    ["provider", "model", "status"],
    registry=metrics_registry,
)

provider_request_duration_seconds = Histogram(
    "free_router_provider_request_duration_seconds",
    "Provider request duration in seconds",
    ["provider", "model"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    registry=metrics_registry,
)

provider_health_score = Gauge(
    "free_router_provider_health_score",
    "Provider health score (0-1)",
    ["provider"],
    registry=metrics_registry,
)

# Quota metrics
quota_used_total = Counter(
    "free_router_quota_used_total",
    "Total quota used",
    ["provider", "model", "tenant"],
    registry=metrics_registry,
)

quota_remaining = Gauge(
    "free_router_quota_remaining",
    "Estimated remaining quota (requests per day)",
    ["provider", "model", "tenant"],
    registry=metrics_registry,
)

# Circuit breaker metrics
circuit_breaker_state = Gauge(
    "free_router_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half-open, 2=open)",
    ["provider", "model"],
    registry=metrics_registry,
)

# Cache metrics
cache_operations_total = Counter(
    "free_router_cache_operations_total",
    "Cache operations",
    ["operation", "result"],
    registry=metrics_registry,
)

cache_entries = Gauge(
    "free_router_cache_entries",
    "Number of cache entries",
    registry=metrics_registry,
)

# Model selection metrics
model_selection_total = Counter(
    "free_router_model_selection_total",
    "Model selection decisions",
    ["provider", "model", "reason"],
    registry=metrics_registry,
)

# Fallback metrics
fallback_total = Counter(
    "free_router_fallback_total",
    "Fallback occurrences",
    ["from_provider", "to_provider", "reason"],
    registry=metrics_registry,
)

# Queue metrics (for backpressure)
queue_size = Gauge(
    "free_router_queue_size",
    "Current request queue size",
    registry=metrics_registry,
)

queue_wait_duration_seconds = Histogram(
    "free_router_queue_wait_duration_seconds",
    "Time spent waiting in queue",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    registry=metrics_registry,
)

# Auth metrics
auth_failures_total = Counter(
    "free_router_auth_failures_total",
    "Authentication failures",
    ["reason"],
    registry=metrics_registry,
)


def render_prometheus() -> bytes:
    """Render metrics in Prometheus format."""
    return generate_latest(metrics_registry)


# ============================================================
# Helper Functions
# ============================================================

def record_http_request(method: str, endpoint: str, status: int, duration: float):
    """Record HTTP request metrics."""
    http_requests_total.labels(method=method, endpoint=endpoint, status=str(status)).inc()
    http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)


def record_provider_request(provider: str, model: str, status: str, duration: float):
    """Record provider request metrics."""
    provider_requests_total.labels(provider=provider, model=model, status=status).inc()
    provider_request_duration_seconds.labels(provider=provider, model=model).observe(duration)


def record_quota_usage(provider: str, model: str, tenant: str, requests: int = 1, tokens: int = 0):
    """Record quota usage."""
    quota_used_total.labels(provider=provider, model=model, tenant=tenant).inc(requests)


def record_cache_operation(operation: str, result: str):
    """Record cache operation."""
    cache_operations_total.labels(operation=operation, result=result).inc()


def record_model_selection(provider: str, model: str, reason: str):
    """Record model selection decision."""
    model_selection_total.labels(provider=provider, model=model, reason=reason).inc()


def record_fallback(from_provider: str, to_provider: str, reason: str):
    """Record fallback occurrence."""
    fallback_total.labels(from_provider=from_provider, to_provider=to_provider, reason=reason).inc()


def set_provider_health(provider: str, score: float):
    """Set provider health score (0-1)."""
    provider_health_score.labels(provider=provider).set(max(0.0, min(1.0, score)))


def set_circuit_breaker_state(provider: str, model: str, state: int):
    """Set circuit breaker state (0=closed, 1=half-open, 2=open)."""
    circuit_breaker_state.labels(provider=provider, model=model).set(state)


def set_queue_size(size: int):
    """Set current queue size."""
    queue_size.set(size)


def record_queue_wait(duration: float):
    """Record queue wait time."""
    queue_wait_duration_seconds.observe(duration)


def record_auth_failure(reason: str):
    """Record authentication failure."""
    auth_failures_total.labels(reason=reason).inc()


# ============================================================
# Structured Logging
# ============================================================

def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Configure structlog for structured JSON logging."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
    ]
    
    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()
    
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Configure stdlib logging
    import logging
    handler = logging.StreamHandler(sys.stdout)
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance."""
    return structlog.get_logger(name)


# ============================================================
# Request ID Middleware
# ============================================================

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add request_id to request state and response headers."""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:12]
        request.state.request_id = request_id
        
        # Add to structlog context
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ============================================================
# OpenTelemetry Tracing
# ============================================================

_tracer_provider: Optional[TracerProvider] = None
_tracer = None


def _make_otlp_span_exporter(endpoint: str, headers: Optional[dict] = None):
    """按 endpoint 协议选择 OTLP exporter（HTTP 优先，gRPC 兜底）。"""
    endpoint = str(endpoint)
    if endpoint.startswith(("http://", "https://")):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HTTPExporter
        return HTTPExporter(endpoint=endpoint, headers=headers or None)
    return OTLPSpanExporter(endpoint=endpoint, headers=headers or None)


def _configure_propagators(names: Optional[list] = None) -> None:
    """按需配置全局传播器（tracecontext / baggage / b3，缺失则跳过）。"""
    if not names:
        return
    try:
        from opentelemetry.propagators.composite import CompositePropagator
    except ImportError:
        return
    import importlib
    comps = []
    for n in names:
        key = str(n).strip().lower()
        spec = {
            "tracecontext": ("opentelemetry.propagators.tracecontext", "TraceContextTextMapPropagator"),
            "w3c": ("opentelemetry.propagators.tracecontext", "TraceContextTextMapPropagator"),
            "baggage": ("opentelemetry.propagators.baggage", "W3CBaggagePropagator"),
            "b3": ("opentelemetry.propagators.b3", "B3MultiFormat"),
        }.get(key)
        if not spec:
            continue
        try:
            cls = getattr(importlib.import_module(spec[0]), spec[1])
            comps.append(cls())
        except Exception:
            continue
    if comps:
        propagate.set_global_textmap(CompositePropagator(comps))


def _init_metrics(endpoint: str, headers: Optional[dict] = None,
                  interval: float = 60.0) -> None:
    """OTLP 指标导出：PeriodicExportingMetricReader（默认 60s 间隔）。"""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    exporter = OTLPMetricExporter(endpoint=str(endpoint), headers=headers or None)
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=max(1000, int(float(interval) * 1000)))
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)


def init_tracing(
    service_name: str = "free-router",
    otlp_endpoint: str = None,
    console_export: bool = False,
    otlp_headers: dict = None,
    sampling_ratio: float = 1.0,
    propagators: list = None,
    metric_export_interval: float = 60.0,
) -> TracerProvider:
    """初始化 OpenTelemetry 追踪 + 传播器 + 指标导出。"""
    global _tracer_provider

    resource = Resource.create({
        "service.name": service_name,
        "service.version": "0.4.0",
    })

    ratio = max(0.0, min(1.0, float(sampling_ratio if sampling_ratio is not None else 1.0)))
    sampler = ParentBased(TraceIdRatioBased(ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)

    if otlp_endpoint:
        exporter = _make_otlp_span_exporter(otlp_endpoint, otlp_headers)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    if console_export or os.environ.get("OTEL_EXPORT_CONSOLE") == "true":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer_provider = provider

    _configure_propagators(propagators)

    if otlp_endpoint:
        try:
            _init_metrics(otlp_endpoint, otlp_headers, metric_export_interval)
        except Exception:
            pass

    return provider


def get_tracer(name: str = "free-router") -> trace.Tracer:
    """Get a tracer instance."""
    return trace.get_tracer(name)


def _fr_tracer():
    """共享的 free-router tracer（惰性创建）。"""
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("free-router")
    return _tracer


def _clean_attrs(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items() if v is not None}


# ---- 自定义 Span 工厂（sync context manager，可同时用于 sync/async 代码）----
def router_select_span(**attrs):
    return _fr_tracer().start_as_current_span(
        "free-router.router.select", attributes=_clean_attrs(**attrs))


def provider_call_span(**attrs):
    return _fr_tracer().start_as_current_span(
        "free-router.provider.call", attributes=_clean_attrs(**attrs))


def cache_span(hit: bool = False):
    name = "free-router.cache.hit" if hit else "free-router.cache.miss"
    return _fr_tracer().start_as_current_span(name)


def circuit_breaker_span(state: str = None):
    return _fr_tracer().start_as_current_span(
        "free-router.circuit-breaker", attributes=_clean_attrs(state=state))


def queue_wait_span():
    return _fr_tracer().start_as_current_span("free-router.queue.wait")


@asynccontextmanager
async def trace_span(tracer: trace.Tracer, name: str, **attributes):
    """Context manager for creating a span."""
    with tracer.start_as_current_span(name) as span:
        for k, v in attributes.items():
            if v is not None:
                span.set_attribute(k, str(v))
        yield span


def _instrument_libraries():
    """按需 instrument 第三方库（可选依赖，缺失则静默跳过）。"""
    _try_instrument("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")
    _try_instrument("opentelemetry.instrumentation.redis", "RedisInstrumentor")
    _try_instrument("opentelemetry.instrumentation.asyncpg", "AsyncPGInstrumentor")


def _try_instrument(module_name: str, cls_name: str):
    import importlib
    try:
        cls = getattr(importlib.import_module(module_name), cls_name)
        cls().instrument()
    except Exception:
        pass


# ============================================================
# Lifecycle Management
# ============================================================

def setup_observability(app, config: dict = None) -> None:
    """Setup all observability components for a FastAPI app."""
    cfg = config or {}
    
    # Logging
    log_level = cfg.get("log_level", "INFO")
    json_logs = cfg.get("json_logs", True)
    configure_logging(log_level, json_logs)
    
    # Request ID middleware
    app.add_middleware(RequestIDMiddleware)
    
    # OpenTelemetry
    otlp_endpoint = cfg.get("otlp_endpoint")
    console_export = cfg.get("console_export", False)
    _configure_propagators(cfg.get("propagators"))
    if otlp_endpoint or console_export:
        init_tracing(
            service_name=cfg.get("service_name", "free-router"),
            otlp_endpoint=otlp_endpoint,
            console_export=console_export,
            otlp_headers=cfg.get("otlp_headers"),
            sampling_ratio=cfg.get("sampling_ratio", 1.0),
            propagators=cfg.get("propagators"),
            metric_export_interval=cfg.get("metric_export_interval", 60),
        )
        FastAPIInstrumentor.instrument_app(app)
        _instrument_libraries()
    
    # Prometheus metrics endpoint
    @app.get("/metrics")
    async def metrics_endpoint():
        return Response(content=render_prometheus(), media_type=CONTENT_TYPE_LATEST)
    
    logger = get_logger("observability")
    logger.info("observability_initialized", 
                log_level=log_level, 
                json_logs=json_logs,
                otlp_enabled=bool(otlp_endpoint))