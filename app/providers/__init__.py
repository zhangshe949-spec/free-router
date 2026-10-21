# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""提供商适配器：统一 BaseAdapter 接口，各家实现 OpenAI 兼容调用。

Phase 0 修复：流式透传。
- chat() 检测 body['stream']，走 httpx 流式（send(stream=True)），
  返回一个异步生成器逐块透传 SSE 字节；非流式仍返回 dict。
- 连接/状态错误在返回生成器之前抛出（ProviderError/QuotaExhausted），
  使 Router 仍能对这些错误做冷却与跨商回退。
"""
import httpx

from ..core.router import ProviderError, QuotaExhausted


class BaseAdapter:
    """统一提供商适配接口"""
    name = "base"

    def __init__(self, pcfg):
        self.pcfg = pcfg
        self.base_url = pcfg.base_url.rstrip("/")

    @property
    def key(self):
        return self.pcfg.resolved_key

    def _headers(self, body=None):
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        # 额外头（如 kilogw 的 Authorization: ''）
        for k, v in (self.pcfg.extra_headers or {}).items():
            h[k] = v
        return h

    def _client(self):
        """可被子类/测试覆盖，便于注入 httpx.MockTransport。"""
        return httpx.AsyncClient(timeout=self.pcfg.request_timeout)

    async def chat(self, body: dict, model: str):
        """向该提供商发起 OpenAI 兼容请求。返回 OpenAI 格式响应。"""
        raise NotImplementedError

    def _build_url(self):
        return f"{self.base_url}/chat/completions"


class OpenAICompatAdapter(BaseAdapter):
    """标准 OpenAI 兼容提供商（AMD/ModelScope/Kilo 网关/新增 7 家国内商）"""
    name = "openai-compat"

    async def chat(self, body: dict, model: str):
        payload = dict(body)
        payload["model"] = model
        url = self._build_url()

        # 流式：返回异步生成器（逐块 SSE 透传）
        if payload.get("stream"):
            return await self._open_stream(url, payload)

        try:
            async with self._client() as client:
                resp = await client.post(url, json=payload,
                                         headers=self._headers(body))
        except httpx.TimeoutException:
            raise ProviderError("timeout", status_code=504, retry_after=30)
        except httpx.ConnectError as e:
            raise ProviderError(f"connect error: {e}", status_code=503)

        if resp.status_code == 429:
            raise QuotaExhausted(f"rate limited: {resp.text[:200]}")
        if resp.status_code != 200:
            raise ProviderError(resp.text[:300], status_code=resp.status_code)
        data = resp.json()
        # 某些聚合网关（Kilo / OpenRouter 风格）会以 HTTP 200 返回错误体：
        # 无 choices 即视为上游失败 —— 必须回退，否则调用方拿到 200 却是错误内容，
        # 且不会触发跨模型顺延（静默失败）。
        if not isinstance(data, dict) or "choices" not in data:
            msg = ""
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    msg = str(err.get("message") or err)
                elif err:
                    msg = str(err)
                elif data.get("message"):
                    msg = str(data.get("message"))
            raise ProviderError(
                f"upstream error payload: {msg or str(data)[:200]}",
                status_code=502)
        return data

    # ---------- 流式 ----------
    async def _open_stream(self, url, payload):
        """建立流式连接：状态错误在此抛出（供 Router 回退），成功后返回生成器。"""
        client = self._client()
        try:
            req = client.build_request("POST", url, json=payload,
                                       headers=self._headers(payload))
            resp = await client.send(req, stream=True)
        except httpx.TimeoutException:
            await client.aclose()
            raise ProviderError("timeout", status_code=504, retry_after=30)
        except httpx.ConnectError as e:
            await client.aclose()
            raise ProviderError(f"connect error: {e}", status_code=503)
        except Exception as e:  # 网络层其它异常 → 可回退
            await client.aclose()
            raise ProviderError(f"stream connect error: {e}", status_code=502)

        if resp.status_code == 429:
            await resp.aclose(); await client.aclose()
            raise QuotaExhausted(f"rate limited (stream): {resp.status_code}")
        if resp.status_code != 200:
            raw = await resp.aread()
            text = raw.decode("utf-8", "ignore")[:300]
            await resp.aclose(); await client.aclose()
            raise ProviderError(text, status_code=resp.status_code)
        return self._stream_iter(resp, client)

    async def _stream_iter(self, resp, client):
        """逐块透传上游 SSE 字节；确保关闭连接。"""
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            try:
                await resp.aclose()
            finally:
                await client.aclose()


class KiloGWAdapter(OpenAICompatAdapter):
    """Kilo 网关：免 key，Authorization 留空"""
    name = "kilogw"


# 工厂
def get_provider_pool(cfg):
    from . import amd, modelscope, kilogw
    pool = {}
    for name, pcfg in cfg.providers.items():
        if name == "amd-radeon":
            pool[name] = amd.AMDAdapter(pcfg)
        elif name == "modelscope":
            pool[name] = modelscope.ModelScopeAdapter(pcfg)
        elif name == "kilogw":
            pool[name] = kilogw.KiloProvider(pcfg)
        else:
            # 未知商统一走 OpenAI 兼容适配器（含新增 7 家国内免费商）
            pool[name] = OpenAICompatAdapter(pcfg)
    return pool