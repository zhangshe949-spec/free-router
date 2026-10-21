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
    key_env: Optional[str] = None
    api_key: Optional[str] = None
    extra_headers: dict = field(default_factory=dict)
    request_timeout: int = 30
    tier_quality: str = "medium"
    models: list = field(default_factory=list)
    # 仅免费模型入池（聚合网关如 Kilo 适用：:free 后缀或 kilo-auto）
    free_only: bool = False
    # 该提供商默认档位（free|paid）；模型级可覆盖。免费池默认自由收费
    type: str = "free"
    tags: list = field(default_factory=list)

    @property
    def resolved_key(self) -> Optional[str]:
        return _resolve_key(self.key_env, self.api_key)


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
    cooldown_429: int = 60
    cooldown_429_escalate: list = field(default_factory=lambda: [300, 1800])
    cooldown_5xx: int = 30
    cooldown_persist_db: bool = True
    db_path: str = "data/free_router.db"
    # 额度预测（P3）：按近期速率预估剩余额度，提前避开即将耗尽的模型
    predict_window_seconds: int = 30
    predict_threshold_seconds: int = 20


@dataclass
class LifecycleCfg:
    refresh_interval_hours: int = 12
    enable_discovery: bool = True
    discovery_urls: dict = field(default_factory=dict)


@dataclass
class Config:
    server_host: str = "127.0.0.1"
    server_port: int = 8787
    openai_base: str = "/v1"
    providers: dict = field(default_factory=dict)   # name -> ProviderCfg
    router: RouterCfg = field(default_factory=RouterCfg)
    quota: QuotaCfg = field(default_factory=QuotaCfg)
    lifecycle: LifecycleCfg = field(default_factory=LifecycleCfg)
    logging_level: str = "INFO"
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
        logging_level=raw.get("logging", {}).get("level", "INFO"),
    )

    # providers
    for name, p in (raw.get("providers") or {}).items():
        cfg.providers[name] = ProviderCfg(
            name=name,
            base_url=p.get("base_url", ""),
            key_env=p.get("key_env"),
            api_key=p.get("api_key"),
            extra_headers=p.get("extra_headers", {}) or {},
            request_timeout=int(p.get("request_timeout", 30)),
            tier_quality=p.get("tier_quality", "medium"),
            models=p.get("models", []) or [],
            free_only=bool(p.get("free_only", False)),
            type=str(p.get("type", "free")),
            tags=p.get("tags", []) or [],
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
                key_env=_blk.get("key_env"), api_key=None,
                extra_headers=_blk.get("extra_headers", {}) or {},
                request_timeout=int(_blk.get("request_timeout", 30)),
                tier_quality=_blk.get("tier_quality", "medium"),
                models=_models, free_only=bool(_blk.get("free_only", False)),
                type=str(_blk.get("type", "free")), tags=_blk.get("tags", []) or [],
            )
    cfg.disabled_models = {(d.get("provider"), d.get("model"))
                           for d in (ov.get("disabled") or [])}
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
        cooldown_429=int(q.get("cooldown_429", 60)),
        cooldown_429_escalate=q.get("cooldown_429_escalate", [300, 1800]),
        cooldown_5xx=int(q.get("cooldown_5xx", 30)),
        cooldown_persist_db=q.get("cooldown_persist_db", True),
        db_path=q.get("db_path", "data/free_router.db"),
        predict_window_seconds=int(q.get("predict_window_seconds", 30)),
        predict_threshold_seconds=int(q.get("predict_threshold_seconds", 20)),
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

    return cfg