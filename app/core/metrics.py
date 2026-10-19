"""指标采集（P5）：纯标准库，暴露 Prometheus 文本格式，无需 prometheus_client。

端点：GET /metrics
用法：router/main 里调用 count_request / observe_latency / count_fallback / ...
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Tuple

_LOCK = threading.Lock()
_REQUESTS: Dict[Tuple[str, str, str], int] = defaultdict(int)      # (provider,model,status)
_QUOTA: Dict[Tuple[str, str], int] = defaultdict(int)              # (provider,model)
_FALLBACK: Dict[Tuple[str, str], int] = defaultdict(int)           # (from_provider,to_provider)
_LATENCY: Dict[Tuple[str, str], list] = defaultdict(list)          # (provider,model) -> [ms...]
_CACHE: Dict[str, int] = defaultdict(int)                          # hit/miss/stale
_STARTED = time.time()
_BUCKETS = [50, 100, 250, 500, 1000, 2000, 5000, 10000]            # ms


def count_request(provider: str, model: str, status: str = "success") -> None:
    with _LOCK:
        _REQUESTS[(provider, model, status)] += 1


def observe_latency(provider: str, model: str, ms: float) -> None:
    with _LOCK:
        bucket = _LATENCY[(provider, model)]
        bucket.append(float(ms))
        if len(bucket) > 500:            # 只留最近 500 次，避免无限增长
            del bucket[:-500]


def count_quota(provider: str, model: str) -> None:
    with _LOCK:
        _QUOTA[(provider, model)] += 1


def count_fallback(from_provider: str, to_provider: str = "") -> None:
    with _LOCK:
        _FALLBACK[(from_provider, to_provider)] += 1


def count_cache(kind: str) -> None:
    """kind: hit | miss | stale"""
    with _LOCK:
        _CACHE[kind] += 1


def _esc(s: str) -> str:
    return str(s).replace('"', '\\"')


def render_prometheus() -> str:
    """输出 Prometheus exposition 文本（无依赖）。"""
    with _LOCK:
        lines = [
            "# HELP free_router_uptime_seconds 服务运行时长",
            "# TYPE free_router_uptime_seconds gauge",
            f"free_router_uptime_seconds {time.time() - _STARTED:.0f}",
            "# HELP free_router_requests_total 请求总数（按提供商/模型/状态）",
            "# TYPE free_router_requests_total counter",
        ]
        for (prov, model, status), n in sorted(_REQUESTS.items()):
            lines.append(f'free_router_requests_total{{provider="{_esc(prov)}",'
                         f'model="{_esc(model)}",status="{_esc(status)}"}} {n}')
        lines += [
            "# HELP free_router_quota_used_total 成功调用消耗的额度计数",
            "# TYPE free_router_quota_used_total counter",
        ]
        for (prov, model), n in sorted(_QUOTA.items()):
            lines.append(f'free_router_quota_used_total{{provider="{_esc(prov)}",'
                         f'model="{_esc(model)}"}} {n}')
        lines += [
            "# HELP free_router_fallback_switches_total 因限流/故障切换次数",
            "# TYPE free_router_fallback_switches_total counter",
        ]
        for (src, dst), n in sorted(_FALLBACK.items()):
            lines.append(f'free_router_fallback_switches_total{{from="{_esc(src)}",'
                         f'to="{_esc(dst)}"}} {n}')
        lines += [
            "# HELP free_router_cache_events_total 缓存事件",
            "# TYPE free_router_cache_events_total counter",
        ]
        for kind, n in sorted(_CACHE.items()):
            lines.append(f'free_router_cache_events_total{{kind="{kind}"}} {n}')
        # 延迟直方图（累计计数）
        lines += [
            "# HELP free_router_request_latency_ms 请求延迟直方图（毫秒）",
            "# TYPE free_router_request_latency_ms histogram",
        ]
        for (prov, model), samples in sorted(_LATENCY.items()):
            for b in _BUCKETS:
                c = sum(1 for x in samples if x <= b)
                lines.append(f'free_router_request_latency_ms_bucket'
                             f'{{provider="{_esc(prov)}",model="{_esc(model)}",'
                             f'le="{b}"}} {c}')
            lines.append(f'free_router_request_latency_ms_bucket'
                         f'{{provider="{_esc(prov)}",model="{_esc(model)}",le="+Inf"}} '
                         f'{len(samples)}')
            lines.append(f'free_router_request_latency_ms_sum'
                         f'{{provider="{_esc(prov)}",model="{_esc(model)}"}} '
                         f'{sum(samples):.1f}')
            lines.append(f'free_router_request_latency_ms_count'
                         f'{{provider="{_esc(prov)}",model="{_esc(model)}"}} '
                         f'{len(samples)}')
    return "\n".join(lines) + "\n"


def snapshot() -> dict:
    with _LOCK:
        return {
            "requests": {f"{p}/{m}/{s}": n for (p, m, s), n in _REQUESTS.items()},
            "fallback_switches": {f"{a}->{b}": n for (a, b), n in _FALLBACK.items()},
            "cache_events": dict(_CACHE),
        }