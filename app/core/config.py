# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""配置加载：config.yaml + 环境变量(.env)"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import os
import yaml

from . import crypto


def _load_dotenv(path: Path):
    """极简 .env 加载（不依赖第三方库）。

    空值占位（如 `AMD_API_KEY=`）直接跳过：否则 setdefault 会把空的占位写进
    环境，阻断后续 .env 兜底来源（多来源 key 时尤其重要）。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if not v:
            continue
        os.environ.setdefault(k, v)


def _resolve_key(key_env, api_key: Optional[str]) -> Optional[str]:
    """取 key 优先级：api_key 直接写 > 环境变量。

    key_env 支持多候选（str / 逗号分隔 / 列表）：按顺序取第一个非空的，
    便于兼容不同工具的键名（如 RADEON_API_KEY / AMD_API_KEY）。
    安全约定：config.yaml 中的 api_key 应保持空字符串，密钥从 .env / 环境变量
    读取；若以 'enc:' 前缀书写，则在此处做 AES 解密（见 crypto.py）。
    """
    if api_key:
        return crypto.maybe_decrypt(api_key)
    names = []
    if isinstance(key_env, (list, tuple)):
        names = [str(n).strip() for n in key_env if str(n).strip()]
    elif isinstance(key_env, str):
        names = [n.strip() for n in key_env.split(",") if n.strip()]
    for n in names:
        env_val = os.environ.get(n) or ""
        if env_val:
            return crypto.maybe_decrypt(env_val)
    return ""


@dataclass
class ProviderCfg:
    name: str
    base_url: str
    # 多 Key 支持：key_env 可为 str / 逗号分隔 / 列表；向后兼容单 key_env
    # 也可显式写 keys_env: [KEY1, KEY2, ...] 或 keys: [{env: X, label: "main"}, ...]
    key_env: Optional[str] = None
    keys_env: list = field(default_factory=list)      # 显式多 Key 列表（环境变量名）
    api_key: Optional[str] = None
    extra_headers: dict = field(default_factory=dict)
    request_timeout: int = 30
    tier_quality: str = "medium"
    models: list = field(default_factory=list)
    free_only: bool = False
    type: str = "free"
    tags: list = field(default_factory=list)
    # Key 故障转移策略：failover(顺序) | round_robin | least_used
    key_strategy: str = "failover"

    @property
    def resolved_keys(self) -> list[str]:
        """返回解析后的所有有效 Key 列表（按配置顺序）。"""
        keys = []
        # 1. keys_env 显式列表（最高优先级）
        for n in self.keys_env:
            val = os.environ.get(n) or ""
            if val:
                keys.append(crypto.maybe_decrypt(val))
        # 2. 兼容旧 key_env（str / 逗号分隔 / 列表）
        if not keys and self.key_env:
            names = []
            if isinstance(self.key_env, (list, tuple)):
                names = [str(n).strip() for n in self.key_env if str(n).strip()]
            elif isinstance(self.key_env, str):
                names = [n.strip() for n in self.key_env.split(",") if n.strip()]
            for n in names:
                val = os.environ.get(n) or ""
                if val:
                    keys.append(crypto.maybe_decrypt(val))
        # 3. api_key 直接写（单 key，向后兼容）
        if not keys and self.api_key:
            keys.append(crypto.maybe_decrypt(self.api_key))
        return keys

    @property
    def resolved_key(self) -> Optional[str]:
        """向后兼容：返回第一个可用 Key。"""
        keys = self.resolved_keys
        return keys[0] if keys else None


@dataclass
class RouterCfg:
    max_retries: int = 20
    sticky_ttl_seconds: int = 1800
    prefer_vision_for_unknown: bool = True
    provider_order: list = field(default_factory=list)
    # 质量分级加权（用户策略：高质量大模型优先，免费小模型只做兜底）
    tier_boost: dict = field(default_factory=lambda: {
        "high": 200.0, "medium": 100.0, "low": 0.0})
    # 任务感知选择器（P0）与可插拔策略（P7）
    use_selector: bool = True
    policy: dict = field(default_factory=dict)


@dataclass
class QuotaCfg:
    rpm_limit: int = 60
    rpd_limit: int = 1000
    tpm_limit: int = 200000
    tpd_limit: int = 5000000
    # 轮转时长：429 只短暂隔离，尽快让恢复的模型回到候选（避免长时间空等）
    cooldown_429: int = 30
    cooldown_429_escalate: list = field(default_factory=lambda: [60, 300])
    cooldown_5xx: int = 20
    cooldown_persist_db: bool = True
    db_path: str = "data/free_router.db"
    # 额度预测（P3）：按近期速率预估剩余额度，提前避开即将耗尽的模型
    predict_window_seconds: int = 30
    predict_threshold_seconds: int = 20
    # 内部守护：每 N 秒巡检一次；冷却一到期立即重新入池（常用模型恢复后第一时间切回）
    recovery_check_seconds: int = 15
    # 冷却比例超过该值时，自动触发一次模型重新发现
    cooled_ratio_trigger: float = 0.6
    # 巡检时对「超过 N 秒没探测过」的提供商做一次连通性探测
    probe_stale_seconds: int = 300


@dataclass
class LifecycleCfg:
    refresh_interval_hours: int = 12
    enable_discovery: bool = True
    discovery_urls: dict = field(default_factory=dict)


@dataclass
class AutofreeCfg:
    """Autofree 自动调度配置"""
    enabled: bool = True
    top_n: int = 10
    fallback_n: int = 10
    recompute_interval_hours: int = 1
    sticky_ttl_seconds: int = 60
    max_retries: int = 30
    min_runtime_requests: int = 5


@dataclass
class SmartFreeCfg:
    """Smart-Free 智能路由配置"""
    enabled: bool = True
    load_balance_strategy: str = "weighted_round_robin"  # round_robin | weighted_round_robin | least_connections | health_first
    sticky_ttl_seconds: int = 300
    max_retries: int = 50
    min_runtime_requests: int = 5


@dataclass
class SustainabilityCfg:
    """可持续性管理器配置"""
    enabled: bool = True
    global_quota_threshold: float = 0.15  # 全局剩余额度低于此阈值时触发告警
    health_check_interval: int = 60
    auto_failover: bool = True
    learning_enabled: bool = True


@dataclass
class ObservabilityCfg:
    """Observability configuration (logging, tracing, metrics)."""
    log_level: str = "INFO"
    json_logs: bool = True
    service_name: str = "free-router"
    otlp_endpoint: Optional[str] = None
    console_export: bool = False
    otlp_headers: dict = field(default_factory=dict)
    sampling_ratio: float = 1.0
    propagators: list = field(default_factory=lambda: ["tracecontext", "baggage"])
    metric_export_interval: float = 60.0
    # Metrics
    metrics_enabled: bool = True
    metrics_port: int = 9090
    # Health checks
    health_check_enabled: bool = True
    health_check_interval_seconds: float = 30.0
    health_check_timeout_seconds: float = 5.0
    # Circuit breaker
    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: float = 0.5
    circuit_breaker_success_threshold: int = 3
    circuit_breaker_window_size: int = 100
    circuit_breaker_window_time: float = 60.0
    # Request queue (backpressure)
    queue_enabled: bool = True
    queue_max_size: int = 50
    queue_max_wait_time: float = 30.0


@dataclass
class Config:
    server_host: str = "127.0.0.1"
    server_port: int = 8787
    openai_base: str = "/v1"
    # 对外只暴露一个模型别名（智能体侧只看到一个模型，实际由本服务自动轮转）
    model_alias: str = "free-router"
    expose_single_model: bool = True
    alias_show_count: bool = True
    # 用户收藏（常用/优先）模型：「provider/model」列表，越靠前越优先
    favorites: list = field(default_factory=list)
    providers: dict = field(default_factory=dict)   # name -> ProviderCfg
    router: RouterCfg = field(default_factory=RouterCfg)
    quota: QuotaCfg = field(default_factory=QuotaCfg)
    lifecycle: LifecycleCfg = field(default_factory=LifecycleCfg)
    observability: ObservabilityCfg = field(default_factory=ObservabilityCfg)
    logging_level: str = "INFO"
    logging_file: str = ""
    # P1 覆盖层：被禁用的模型 {(provider, model)}
    disabled_models: set = field(default_factory=set)
    # P2 缓存容灾
    cache: dict = field(default_factory=lambda: {
        "enabled": True, "ttl_seconds": 86400, "max_entries": 2000,
        "serve_hits": False})
    # P5 指标
    metrics: dict = field(default_factory=lambda: {"enabled": True})
    # P6 告警
    alerter: dict = field(default_factory=dict)
    # 可选网关访问密钥（WebSocket / 批量等端点鉴权用；空 = 不鉴权）
    access_key: str = ""
    # P9 插件框架：[{name, module, class, config}]
    plugins: list = field(default_factory=list)
    # P9 鉴权：{api_keys: [], jwt: {}, rbac: {}}
    auth: dict = field(default_factory=dict)


def load_config(path: Path, load_env_files: bool = True) -> Config:
    """读取配置。

    load_env_files=False 时完全跳过 .env 加载（不读本地/全局密钥文件），
    仅使用进程环境里已存在的变量。单元测试用它把测试与开发机真实凭据
    隔离——否则测试会依赖 ~/.free-router/.env，在无凭据的 CI 上全部失败。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    # 加载 .env：先本目录（优先），再 config.yaml 里 env_files 指定的兜底来源
    # （如 ~/.free-router/.env，避免同一套 key 在多处重复维护）
    if load_env_files:
        _load_dotenv(path.parent / ".env")
        for extra in (raw.get("env_files") or []):
            p = Path(str(extra)).expanduser()          # 支持 ~/.free-router/.env
            if not p.is_absolute():
                p = path.parent.parent / p
            if p.exists():                             # 缺失的兜底来源静默跳过
                _load_dotenv(p)

    server = raw.get("server", {})
    cfg = Config(
        server_host=server.get("host", "127.0.0.1"),
        server_port=int(server.get("port", 8787)),
        openai_base=server.get("openai_base", "/v1"),
        model_alias=str(server.get("model_alias", "free-router") or "free-router"),
        expose_single_model=bool(server.get("expose_single_model", True)),
        alias_show_count=bool(server.get("alias_show_count", True)),
        access_key=str(server.get("access_key", "") or ""),
        logging_level=raw.get("logging", {}).get("level", "INFO"),
        logging_file=str(path.parent.parent / raw.get("logging", {}).get("file", "")) if raw.get("logging", {}).get("file") else "",
    )

    # providers
    for name, p in (raw.get("providers") or {}).items():
        cfg.providers[name] = ProviderCfg(
            name=name,
            base_url=p.get("base_url", ""),
            key_env=p.get("key_env"),
            keys_env=p.get("keys_env", []) or [],
            api_key=p.get("api_key"),
            extra_headers=p.get("extra_headers", {}) or {},
            request_timeout=int(p.get("request_timeout", 30)),
            tier_quality=p.get("tier_quality", "medium"),
            models=p.get("models", []) or [],
            free_only=bool(p.get("free_only", False)),
            type=str(p.get("type", "free")),
            tags=p.get("tags", []) or [],
            key_strategy=str(p.get("key_strategy", "failover")),
        )

    # ---- P1 覆盖层：合并运行期动态增删的提供商/模型（不动 config.yaml）----
    from . import overlay as _ov
    ov = _ov.load_overlay(path)
    for _name, _blk in (ov.get("providers") or {}).items():
        _models = _blk.get("models") or []
        if _name in cfg.providers:
            pc = cfg.providers[_name]
            if _blk.get("base_url"):
                pc.base_url = _blk["base_url"]
            for _k in ("key_env", "type", "tier_quality"):
                if _blk.get(_k) is not None:
                    setattr(pc, _k, _blk[_k])
            if _blk.get("keys_env") is not None:
                pc.keys_env = _blk["keys_env"] or []
            if _blk.get("key_strategy") is not None:
                pc.key_strategy = str(_blk["key_strategy"])
            if _blk.get("free_only") is not None:
                pc.free_only = bool(_blk["free_only"])
            if _blk.get("request_timeout") is not None:
                pc.request_timeout = int(_blk["request_timeout"])
            if _blk.get("extra_headers"):
                pc.extra_headers = {**(pc.extra_headers or {}), **_blk["extra_headers"]}
            for _m in _models:
                _ids = [mm.get("id") for mm in pc.models]
                if _m.get("id") in _ids:
                    pc.models = [_m if mm.get("id") == _m.get("id") else mm
                                 for mm in pc.models]
                else:
                    pc.models.append(_m)
        else:
            cfg.providers[_name] = ProviderCfg(
                name=_name, base_url=_blk.get("base_url", ""),
                key_env=_blk.get("key_env"),
                keys_env=_blk.get("keys_env", []) or [],
                api_key=None,
                extra_headers=_blk.get("extra_headers", {}) or {},
                request_timeout=int(_blk.get("request_timeout", 30)),
                tier_quality=_blk.get("tier_quality", "medium"),
                models=_models, free_only=bool(_blk.get("free_only", False)),
                type=str(_blk.get("type", "free")), tags=_blk.get("tags", []) or [],
                key_strategy=str(_blk.get("key_strategy", "failover")),
            )
    cfg.disabled_models = {(d.get("provider"), d.get("model"))
                           for d in (ov.get("disabled") or [])}
    cfg.favorites = [str(x) for x in (ov.get("favorites") or []) if x]
    for pc in cfg.providers.values():
        if cfg.disabled_models:
            pc.models = [m for m in pc.models
                         if (pc.name, m.get("id")) not in cfg.disabled_models]

    # router
    r = raw.get("router", {}) or {}
    cfg.router = RouterCfg(
        max_retries=int(r.get("max_retries", 20)),
        sticky_ttl_seconds=int(r.get("sticky_ttl_seconds", 1800)),
        prefer_vision_for_unknown=r.get("prefer_vision_for_unknown", True),
        provider_order=r.get("provider_order", []) or list(cfg.providers.keys()),
        use_selector=bool(r.get("use_selector", True)),
        policy=r.get("policy", {}) or {},
    )

    # quota
    q = raw.get("quota", {}) or {}
    cfg.quota = QuotaCfg(
        rpm_limit=int(q.get("rpm_limit", 60)),
        rpd_limit=int(q.get("rpd_limit", 1000)),
        tpm_limit=int(q.get("tpm_limit", 200000)),
        tpd_limit=int(q.get("tpd_limit", 5000000)),
        cooldown_429=int(q.get("cooldown_429", 30)),
        cooldown_429_escalate=q.get("cooldown_429_escalate", [60, 300]),
        cooldown_5xx=int(q.get("cooldown_5xx", 20)),
        cooldown_persist_db=q.get("cooldown_persist_db", True),
        db_path=q.get("db_path", "data/free_router.db"),
        predict_window_seconds=int(q.get("predict_window_seconds", 30)),
        predict_threshold_seconds=int(q.get("predict_threshold_seconds", 20)),
        recovery_check_seconds=int(q.get("recovery_check_seconds", 15)),
        cooled_ratio_trigger=float(q.get("cooled_ratio_trigger", 0.6)),
        probe_stale_seconds=int(q.get("probe_stale_seconds", 300)),
    )
    # db 路径：相对路径按配置文件所在项目根解析（skill 副本可独立运行）
    db = Path(cfg.quota.db_path)
    if not db.is_absolute():
        cfg.quota.db_path = str((path.parent.parent / db).resolve())

    # lifecycle
    lc = raw.get("lifecycle", {}) or {}
    cfg.lifecycle = LifecycleCfg(
        refresh_interval_hours=int(lc.get("refresh_interval_hours", 12)),
        enable_discovery=lc.get("enable_discovery", True),
        discovery_urls=lc.get("discovery_urls", {}) or {},
    )

    # autofree
    af = raw.get("autofree", {}) or {}
    cfg.autofree = AutofreeCfg(
        enabled=bool(af.get("enabled", True)),
        top_n=int(af.get("top_n", 10)),
        fallback_n=int(af.get("fallback_n", 10)),
        recompute_interval_hours=int(af.get("recompute_interval_hours", 1)),
        sticky_ttl_seconds=int(af.get("sticky_ttl_seconds", 60)),
        max_retries=int(af.get("max_retries", 30)),
        min_runtime_requests=int(af.get("min_runtime_requests", 5)),
    )

    # smart-free
    sf = raw.get("smart_free", {}) or {}
    cfg.smart_free = SmartFreeCfg(
        enabled=bool(sf.get("enabled", True)),
        load_balance_strategy=str(sf.get("load_balance_strategy", "weighted_round_robin")),
        sticky_ttl_seconds=int(sf.get("sticky_ttl_seconds", 300)),
        max_retries=int(sf.get("max_retries", 50)),
        min_runtime_requests=int(sf.get("min_runtime_requests", 5)),
    )

    # sustainability
    sus = raw.get("sustainability", {}) or {}
    cfg.sustainability = SustainabilityCfg(
        enabled=bool(sus.get("enabled", True)),
        global_quota_threshold=float(sus.get("global_quota_threshold", 0.15)),
        health_check_interval=int(sus.get("health_check_interval", 60)),
        auto_failover=bool(sus.get("auto_failover", True)),
        learning_enabled=bool(sus.get("learning_enabled", True)),
    )

    # observability
    obs = raw.get("observability", {}) or {}
    cfg.observability = ObservabilityCfg(
        log_level=obs.get("log_level", "INFO"),
        json_logs=obs.get("json_logs", True),
        service_name=obs.get("service_name", "free-router"),
        otlp_endpoint=obs.get("otlp_endpoint"),
        console_export=obs.get("console_export", False),
        otlp_headers=obs.get("otlp_headers", {}) or {},
        sampling_ratio=float(obs.get("sampling_ratio", 1.0)),
        propagators=obs.get("propagators", ["tracecontext", "baggage"]) or [],
        metric_export_interval=float(obs.get("metric_export_interval", 60)),
        metrics_enabled=obs.get("metrics_enabled", True),
        metrics_port=int(obs.get("metrics_port", 9090)),
        health_check_enabled=obs.get("health_check_enabled", True),
        health_check_interval_seconds=float(obs.get("health_check_interval_seconds", 30.0)),
        health_check_timeout_seconds=float(obs.get("health_check_timeout_seconds", 5.0)),
        circuit_breaker_enabled=obs.get("circuit_breaker_enabled", True),
        circuit_breaker_failure_threshold=float(obs.get("circuit_breaker_failure_threshold", 0.5)),
        circuit_breaker_success_threshold=int(obs.get("circuit_breaker_success_threshold", 3)),
        circuit_breaker_window_size=int(obs.get("circuit_breaker_window_size", 100)),
        circuit_breaker_window_time=float(obs.get("circuit_breaker_window_time", 60.0)),
        queue_enabled=obs.get("queue_enabled", True),
        queue_max_size=int(obs.get("queue_max_size", 50)),
        queue_max_wait_time=float(obs.get("queue_max_wait_time", 30.0)),
    )

    # P2/P5/P6：缓存 / 指标 / 告警
    cache = raw.get("cache", {}) or {}
    cfg.cache = {
        "enabled": bool(cache.get("enabled", True)),
        "ttl_seconds": int(cache.get("ttl_seconds", 86400)),
        "max_entries": int(cache.get("max_entries", 2000)),
        "serve_hits": bool(cache.get("serve_hits", False)),
    }
    metrics = raw.get("metrics", {}) or {}
    cfg.metrics = {"enabled": bool(metrics.get("enabled", True))}
    cfg.alerter = raw.get("alerter", {}) or {}
    cfg.plugins = raw.get("plugins", []) or {}
    cfg.auth = raw.get("auth", {}) or {}

    return cfg