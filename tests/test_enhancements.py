# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""可选增强测试：Helm Chart / MkDocs / OpenTelemetry / 鉴权。

全部为结构性校验 + 无网络的单元测试。
"""
import sys
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import yaml

from app.core.config import load_config
from app.core import auth as auth_mod
from app.core.auth import AuthEngine, AuthMiddleware, AuthContext, extract_token

ROOT = Path(__file__).parent.parent
CFG_PATH = ROOT / "config" / "config.yaml"


@pytest.fixture
def cfg():
    return load_config(CFG_PATH, load_env_files=False)


@pytest.fixture(autouse=True)
def _reset_auth_engine():
    yield
    auth_mod.set_engine(None)


# ============================================================
# 1. Helm Chart
# ============================================================
def test_helm_chart_yaml_valid():
    chart = ROOT / "charts" / "free-router"
    assert (chart / "Chart.yaml").exists()
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
    assert chart_yaml["apiVersion"] == "v2"
    assert chart_yaml["name"] == "free-router"
    assert chart_yaml["version"] == "0.4.0"
    assert chart_yaml["appVersion"] == "0.4.0"


def test_helm_values_complete():
    chart = ROOT / "charts" / "free-router"
    vals = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    for key in ("replicaCount", "image", "service", "resources", "autoscaling",
                "ingress", "config", "secret", "serviceMonitor",
                "podDisruptionBudget", "securityContext", "affinity",
                "tolerations", "probes"):
        assert key in vals, f"values.yaml 缺少 {key}"
    # config 可覆盖 config.yaml 任意字段
    assert "server" in vals["config"]
    assert "observability" in vals["config"]
    assert "auth" in vals["config"]


def test_helm_templates_present():
    tpl = ROOT / "charts" / "free-router" / "templates"
    for name in ("deployment.yaml", "service.yaml", "configmap.yaml",
                 "secret.yaml", "servicemonitor.yaml", "pdb.yaml",
                 "ingress.yaml", "_helpers.tpl", "hpa.yaml"):
        assert (tpl / name).exists(), f"缺少模板 {name}"


def test_helm_helpers_define_all_used():
    tpl = ROOT / "charts" / "free-router" / "templates"
    helpers = (tpl / "_helpers.tpl").read_text(encoding="utf-8")
    for h in ("free-router.name", "free-router.fullname", "free-router.chart",
              "free-router.labels", "free-router.selectorLabels",
              "free-router.serviceAccountName", "free-router.secretName"):
        assert f'define "{h}"' in helpers, f"_helpers.tpl 缺 {h}"


# ============================================================
# 2. MkDocs
# ============================================================
def test_mkdocs_yml_valid_and_nav_resolves():
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    # mkdocs 的 !!python/name 标签 SafeLoader 无法解析，先替换为普通字符串
    text = text.replace("!!python/name:pymdownx.superfences.fence_code_format",
                        "pymdownx.superfences.fence_code_format")
    mk = yaml.safe_load(text)
    assert mk["site_name"] == "Free-Router"
    assert mk["theme"]["name"] == "material"

    def walk(items):
        for it in items:
            if isinstance(it, str):
                yield it
            elif isinstance(it, dict):
                for k, v in it.items():
                    yield from walk([v])

    for ref in walk(mk["nav"]):
        f = ROOT / "docs" / ref
        assert f.exists(), f"nav 引用的文档不存在: {ref}"


def test_mkdocs_docs_present():
    docs = ROOT / "docs"
    for name in ("index.md", "configuration.md", "api-reference.md",
                 "providers.md", "observability.md", "plugins.md",
                 "deployment.md", "faq.md"):
        assert (docs / name).exists(), f"缺少文档 {name}"
    for ex in ("python.md", "javascript.md", "go.md"):
        assert (docs / "examples" / ex).exists(), f"缺少示例 {ex}"


# ============================================================
# 3. OpenTelemetry
# ============================================================
def test_observability_config_fields(cfg):
    obs = cfg.observability
    assert hasattr(obs, "otlp_headers")
    assert hasattr(obs, "sampling_ratio")
    assert obs.sampling_ratio == 1.0
    assert "tracecontext" in obs.propagators
    assert obs.metric_export_interval == 60


def test_span_helpers_exist_and_usable():
    from app.core.observability import (
        router_select_span, provider_call_span, cache_span,
        circuit_breaker_span, queue_wait_span,
    )
    with router_select_span(candidates=3):
        pass
    with provider_call_span(provider="p", model="m"):
        pass
    with cache_span(hit=True):
        pass
    with circuit_breaker_span(state="open"):
        pass
    with queue_wait_span():
        pass


def test_init_tracing_signature_accepts_new_kwargs():
    import inspect
    from app.core.observability import init_tracing
    sig = inspect.signature(init_tracing)
    for kw in ("otlp_headers", "sampling_ratio", "propagators",
               "metric_export_interval"):
        assert kw in sig.parameters, f"init_tracing 缺参数 {kw}"


# ============================================================
# 4. 鉴权
# ============================================================
def test_extract_token_bearer_and_xapikey():
    assert extract_token({"authorization": "Bearer abc"}) == "abc"
    assert extract_token({"authorization": "bearer abc"}) == "abc"
    assert extract_token({"X-API-Key": "xyz"}) == "xyz"
    assert extract_token({}) == ""


def test_auth_engine_api_key_scope():
    cfg = SimpleNamespace(
        access_key="",
        auth={"api_keys": [{"key": "sk-admin", "scopes": ["admin"], "roles": ["admin"]},
                           {"key": "sk-batch", "scopes": ["batch"]}],
              "rbac": {"admin": ["*"], "user": ["chat", "batch", "ws"], "viewer": ["read"]},
              "jwt": {}},
    )
    engine = AuthEngine(cfg)
    assert engine.enabled is True
    assert engine.authorize(engine.resolve("sk-admin"), "/admin/status", "GET")
    assert engine.authorize(engine.resolve("sk-batch"), "/v1/batch/completions", "POST")
    assert not engine.authorize(engine.resolve("sk-batch"), "/admin/status", "GET")
    assert engine.resolve("sk-batch")["key_id"] == "sk-batch"


def test_auth_engine_backward_compat_access_key():
    cfg = SimpleNamespace(access_key="legacy-key", auth={})
    engine = AuthEngine(cfg)
    assert engine.enabled is True
    entry = engine.resolve("legacy-key")
    assert entry is not None
    assert engine.authorize(entry, "/admin/anything", "GET") is True


def test_auth_engine_disabled_when_no_keys():
    cfg = SimpleNamespace(access_key="", auth={"api_keys": [], "jwt": {}, "rbac": {}})
    engine = AuthEngine(cfg)
    assert engine.enabled is False


def test_auth_rbac_viewer_read_only():
    cfg = SimpleNamespace(
        access_key="",
        auth={"api_keys": [{"key": "sk-view", "roles": ["viewer"]}],
              "rbac": {"viewer": ["read"]}, "jwt": {}},
    )
    engine = AuthEngine(cfg)
    entry = engine.resolve("sk-view")
    assert engine.authorize(entry, "/admin/status", "GET") is True
    assert engine.authorize(entry, "/admin/models", "GET") is False
    assert engine.authorize(entry, "/health", "GET") is True


def test_auth_jwt_decode():
    import jwt as pyjwt
    token = pyjwt.encode({"sub": "u1", "tenant": "team-a",
                          "scopes": ["chat"], "roles": ["user"]},
                         key="secret", algorithm="HS256")
    cfg = SimpleNamespace(
        access_key="",
        auth={"api_keys": [], "rbac": {}, "jwt": {"algorithm": "HS256"}},
    )
    engine = AuthEngine(cfg)
    entry = engine.resolve(token)
    assert entry is not None
    assert entry["tenant"] == "team-a"
    assert "chat" in entry["scopes"]
    assert entry["kind"] == "jwt"


def _mk_scope(method, path, headers=None):
    return {"type": "http", "method": method, "path": path,
            "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
            "state": {}}


def test_auth_middleware_401_and_403():
    cfg = SimpleNamespace(
        access_key="",
        auth={"api_keys": [{"key": "sk-batch", "scopes": ["batch"]}],
              "rbac": {}, "jwt": {}},
    )
    engine = AuthEngine(cfg)
    auth_mod.set_engine(engine)

    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = AuthMiddleware(fake_app)

    # 无 token → 401
    captured = {}
    async def send_401(msg):
        captured[msg["type"]] = msg
    scope = _mk_scope("POST", "/v1/chat/completions")
    asyncio.run(mw(scope, None, send_401))
    assert captured["http.response.start"]["status"] == 401

    # 有 token 但无 chat scope → 403
    captured2 = {}
    async def send_403(msg):
        captured2[msg["type"]] = msg
    scope2 = _mk_scope("POST", "/v1/chat/completions",
                       {"authorization": "Bearer sk-batch"})
    asyncio.run(mw(scope2, None, send_403))
    assert captured2["http.response.start"]["status"] == 403

    # batch key 访问 batch → 放行，且注入 auth
    captured3 = {}
    async def send_ok(msg):
        captured3[msg["type"]] = msg
    scope3 = _mk_scope("POST", "/v1/batch/completions",
                       {"authorization": "Bearer sk-batch"})
    asyncio.run(mw(scope3, None, send_ok))
    assert captured3["http.response.start"]["status"] == 200
    assert scope3["state"]["auth"].tenant == "default"
    assert "batch" in scope3["state"]["auth"].scopes


def test_auth_context_dataclass():
    ctx = AuthContext(authenticated=True, tenant="t", scopes=["chat"])
    assert ctx.tenant == "t"
    assert ctx.authenticated is True

# ============================================================
# 5. 赛博朋克主题
# ============================================================
def test_cyberpunk_theme_assets():
    theme = ROOT / "docs" / "theme"
    assert (theme / "main.html").exists(), "缺少 main.html"
    assert (theme / "stylesheets" / "extra.css").exists(), "缺少 extra.css"
    assert (theme / "javascripts" / "extra.js").exists(), "缺少 extra.js"

    css = (theme / "stylesheets" / "extra.css").read_text(encoding="utf-8")
    for var in ("--bg-primary", "--bg-secondary", "--accent-cyan",
                "--accent-magenta", "--accent-gold", "--border-glow",
                "--glitch-speed", "--font-heading", "--font-mono"):
        assert var in css, f"extra.css 缺变量 {var}"

    js = (theme / "javascripts" / "extra.js").read_text(encoding="utf-8")
    for fn in ("initGlitchText", "initParticleGrid", "initCodeCopyToast",
               "initTOCHighlight", "initThemeToggle"):
        assert fn in js, f"extra.js 缺函数 {fn}"

    mk = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "custom_dir: docs/theme" in mk
    assert "theme/stylesheets/extra.css" in mk
    assert "theme/javascripts/extra.js" in mk
    assert "content.code.copy" in mk

# ============================================================
# 6. JWT 公钥验签
# ============================================================
def _make_rsa_jwks(kid="k1"):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    import json as _json
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = _json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return key, {"keys": [jwk]}


def test_jwt_valid_signature_passes(monkeypatch):
    import jwt as pyjwt
    key, jwks = _make_rsa_jwks()
    token = pyjwt.encode({"sub": "u1", "tenant": "team-a", "scopes": ["chat"]},
                         key, algorithm="RS256", headers={"kid": "k1"})
    cfg = SimpleNamespace(
        access_key="",
        auth={"api_keys": [], "rbac": {},
              "jwt": {"algorithm": "RS256", "jwks_url": "https://example/.well-known/jwks.json"}},
    )
    engine = AuthEngine(cfg)
    monkeypatch.setattr(engine, "_fetch_jwks", lambda url: jwks)
    entry = engine.resolve(token)
    assert entry is not None
    assert entry["kind"] == "jwt"
    assert entry["tenant"] == "team-a"
    assert "chat" in entry["scopes"]


def test_jwt_invalid_signature_rejected(monkeypatch):
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    _, jwks = _make_rsa_jwks()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_token = pyjwt.encode({"sub": "evil"}, other_key,
                             algorithm="RS256", headers={"kid": "k1"})
    cfg = SimpleNamespace(
        access_key="",
        auth={"api_keys": [], "rbac": {},
              "jwt": {"algorithm": "RS256", "jwks_url": "https://example/.well-known/jwks.json"}},
    )
    engine = AuthEngine(cfg)
    monkeypatch.setattr(engine, "_fetch_jwks", lambda url: jwks)
    assert engine.resolve(bad_token) is None

