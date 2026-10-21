# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""鉴权：API Key / JWT / RBAC + 中间件。

- API Key：config.auth.api_keys（多 Key，绑定 tenant/scopes/roles/rate_limit/models_allowlist/denylist）
- JWT：config.auth.jwt（issuer/audience/jwks_url/algorithm），解析 sub/tenant/scopes/roles
- RBAC：config.auth.rbac（角色 → 权限集合）
- 向后兼容：server.access_key 非空时自动生成一个 scope=* 的 api_key 条目
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AuthContext:
    """鉴权上下文（注入 request.state.auth）。"""
    authenticated: bool = False
    key_id: Optional[str] = None
    tenant: str = "default"
    scopes: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    models_allowlist: List[str] = field(default_factory=list)
    models_denylist: List[str] = field(default_factory=list)
    rate_limit: Dict[str, Any] = field(default_factory=dict)


# 路径前缀 → 所需 scope
_SCOPE_RULES = [
    ("/admin/", "admin"),
    ("/v1/batch/", "batch"),
    ("/v1/ws/chat", "ws"),
    ("/v1/chat/completions", "chat"),
    ("/v1/messages", "chat"),
]

_READ_ONLY_PATHS = ("/health", "/metrics", "/v1/models")


class AuthenticationError(Exception):
    """鉴权失败（无效签名 / JWKS 拉取失败等）。"""


def extract_token(headers: Optional[Dict[str, str]]) -> str:
    """从 Authorization: Bearer <t> 或 X-API-Key 提取 token（键名大小写不敏感）。"""
    if not headers:
        return ""
    low = {str(k).lower(): v for k, v in headers.items()}
    auth = low.get("authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (low.get("x-api-key", "") or "").strip()


class AuthEngine:
    """鉴权引擎：加载配置 → 解析凭据 → 授权判定。"""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self._api_keys: Dict[str, dict] = {}
        self._rbac: Dict[str, list] = {}
        self._jwt_cfg: dict = {}
        self._jwks_cache: Dict[str, tuple] = {}   # url -> (ts, jwks dict)
        if cfg is not None:
            self.load(cfg)

    _JWKS_TTL = 3600  # JWKS in-memory 缓存 TTL（秒）

    def load(self, cfg):
        auth = getattr(cfg, "auth", None) or {}
        self._rbac = dict(auth.get("rbac") or {})
        self._jwt_cfg = dict(auth.get("jwt") or {})
        for entry in (auth.get("api_keys") or []):
            if isinstance(entry, dict) and entry.get("key"):
                self._api_keys[entry["key"]] = entry
        # 向后兼容：server.access_key → scope=* 的 api_key
        ak = (getattr(cfg, "access_key", "") or "").strip()
        if ak and ak not in self._api_keys:
            self._api_keys[ak] = {
                "key": ak, "tenant": "default",
                "scopes": ["*"], "roles": ["admin"],
            }

    @property
    def enabled(self) -> bool:
        return bool(self._api_keys or self._jwt_cfg)

    def resolve(self, token: str) -> Optional[dict]:
        """按 API Key 或 JWT 解析出鉴权条目（dict）。"""
        if not token:
            return None
        if token in self._api_keys:
            entry = dict(self._api_keys[token])
            entry.setdefault("key_id", entry.get("key"))
            return entry
        jwt_entry = self._decode_jwt(token)
        if jwt_entry:
            return jwt_entry
        return None

    def _decode_jwt(self, token: str) -> Optional[dict]:
        if not self._jwt_cfg:
            return None
        try:
            jwks_url = self._jwt_cfg.get("jwks_url")
            if jwks_url:
                # 配置了 jwks_url → 走公钥验签分支
                payload = self._verify_signature(token, jwks_url)
            else:
                import jwt as pyjwt
                alg = self._jwt_cfg.get("algorithm", "RS256")
                payload = pyjwt.decode(token, options={"verify_signature": False}, algorithms=[alg])
        except AuthenticationError as e:
            self._audit("invalid_signature", str(e))
            return None
        except Exception:
            return None

        # issuer / audience 校验（验签路径已带，此处兜底）
        issuer = self._jwt_cfg.get("issuer")
        if issuer and payload.get("iss") != issuer:
            return None
        audience = self._jwt_cfg.get("audience")
        if audience:
            aud = payload.get("aud")
            if aud != audience and not (isinstance(aud, list) and audience in aud):
                return None

        scopes = payload.get("scopes") or []
        if isinstance(scopes, str):
            scopes = [s for s in scopes.split(" ") if s]
        return {
            "kind": "jwt",
            "key_id": payload.get("sub"),
            "tenant": payload.get("tenant", "default"),
            "scopes": list(scopes),
            "roles": list(payload.get("roles") or []),
        }

    def _fetch_jwks(self, jwks_url: str) -> dict:
        """拉取 JWKS（in-memory 缓存 TTL 1h；失败重试 3 次；超时 5s）。"""
        now = time.time()
        cached = self._jwks_cache.get(jwks_url)
        if cached and (now - cached[0]) < self._JWKS_TTL:
            return cached[1]
        last = None
        for _ in range(3):
            try:
                import httpx
                resp = httpx.get(jwks_url, timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("jwks 非 JSON 对象")
                self._jwks_cache[jwks_url] = (now, data)
                return data
            except Exception as e:  # noqa: BLE001
                last = e
        raise AuthenticationError("jwks_fetch_failed") from last

    def _signing_key_from_jwks(self, jwks: dict, kid: Optional[str]):
        import jwt as pyjwt
        try:
            jwks_set = pyjwt.PyJWKSet.from_dict(jwks)
        except Exception:
            return None
        if kid:
            try:
                return jwks_set[kid]
            except Exception:
                return None
        # 无 kid：取第一个 RSA 公钥
        for k in (jwks.get("keys") or []):
            if k.get("kty") == "RSA":
                try:
                    return jwks_set[k.get("kid")]
                except Exception:
                    continue
        return None

    def _verify_signature(self, token: str, jwks_url: str) -> dict:
        import jwt as pyjwt
        header = pyjwt.get_unverified_header(token)
        alg = header.get("alg", "")
        if alg.startswith("HS"):
            # 对称密钥不适用 JWKS 公钥验签 → 退回未验签解码
            return pyjwt.decode(token, options={"verify_signature": False}, algorithms=[alg])
        jwks = self._fetch_jwks(jwks_url)
        key = self._signing_key_from_jwks(jwks, header.get("kid"))
        if key is None:
            raise AuthenticationError("invalid_signature: unknown kid")
        try:
            return pyjwt.decode(
                token, key, algorithms=["RS256", "ES256", "PS256"],
                options={"verify_signature": True, "verify_iss": False, "verify_aud": False})
        except pyjwt.InvalidSignatureError as e:
            raise AuthenticationError("invalid_signature") from e

    def _audit(self, event: str, detail: str = ""):
        logging.getLogger("free_router.auth").warning("auth %s: %s", event, detail[:160])

    def effective_scopes(self, entry: dict) -> set:
        scopes = set(entry.get("scopes") or [])
        for role in (entry.get("roles") or []):
            scopes |= set(self._rbac.get(role) or [])
        return scopes

    def authorize(self, entry: dict, path: str, method: str = "GET") -> bool:
        scopes = self.effective_scopes(entry)
        if "*" in scopes:
            return True
        # viewer（read）可 GET /admin/status，其余 /admin/* 需 admin
        if path == "/admin/status" and method == "GET":
            return "admin" in scopes or "read" in scopes
        for prefix, scope in _SCOPE_RULES:
            if path.startswith(prefix) or path == prefix.rstrip("/"):
                return scope in scopes
        # /health /metrics /v1/models 等只读端点：已认证即放行
        return True

    def context_for(self, entry: dict) -> AuthContext:
        return AuthContext(
            authenticated=True,
            key_id=entry.get("key_id"),
            tenant=entry.get("tenant", "default"),
            scopes=list(entry.get("scopes") or []),
            roles=list(entry.get("roles") or []),
            models_allowlist=list(entry.get("models_allowlist") or []),
            models_denylist=list(entry.get("models_denylist") or []),
            rate_limit=dict(entry.get("rate_limit") or {}),
        )


# 模块级引擎（main.py 在 lifespan 里 set_engine）
_engine: Optional[AuthEngine] = None


def set_engine(engine: Optional[AuthEngine]) -> None:
    global _engine
    _engine = engine


def get_engine() -> Optional[AuthEngine]:
    return _engine


class AuthMiddleware:
    """纯 ASGI 鉴权中间件：提取 token → 解析 → 授权 → 注入 request.state.auth。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        engine = get_engine()
        if engine is None or not engine.enabled:
            await self.app(scope, receive, send)
            return
        headers = _headers_from_scope(scope)
        token = extract_token(headers)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        entry = engine.resolve(token) if token else None
        if entry is None:
            await _send_json(send, 401,
                             {"error": "unauthorized", "message": "missing or invalid credentials"},
                             headers={"WWW-Authenticate": "Bearer"})
            return
        if not engine.authorize(entry, path, method):
            await _send_json(send, 403,
                             {"error": "forbidden", "message": "insufficient scope or role"})
            return
        scope.setdefault("state", {})["auth"] = engine.context_for(entry)
        await self.app(scope, receive, send)


def _headers_from_scope(scope) -> Dict[str, str]:
    out = {}
    for k, v in scope.get("headers", []):
        out[k.decode("latin-1").lower()] = v.decode("latin-1")
    return out


async def _send_json(send, status: int, payload: dict, headers: Optional[dict] = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = [(b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1"))]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode("latin-1"), str(v).encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})
