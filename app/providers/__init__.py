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
        self._key_index = 0  # 当前使用的 key 索引

    @property
    def keys(self) -> list[str]:
        """返回该提供商所有可用 Key 列表。"""
        return self.pcfg.resolved_keys

    @property
    def key(self) -> str:
        """向后兼容：返回当前使用的 Key（第一个可用）。"""
        ks = self.keys
        return ks[0] if ks else ""

    @property
    def key_strategy(self) -> str:
        return getattr(self.pcfg, "key_strategy", "failover")

    def _headers(self, body=None, key: str = None):
        h = {"Content-Type": "application/json"}
        k = key or self.key
        if k:
            h["Authorization"] = f"Bearer {k}"
        for k2, v in (self.pcfg.extra_headers or {}).items():
            h[k2] = v
        return h

    def _client(self):
        """可被子类/测试覆盖，便于注入 httpx.MockTransport。"""
        return httpx.AsyncClient(timeout=self.pcfg.request_timeout)

    def _next_key(self) -> Optional[str]:
        """按策略获取下一个可用 Key。"""
        ks = self.keys
        if not ks:
            return None
        if self.key_strategy == "round_robin":
            k = ks[self._key_index % len(ks)]
            self._key_index = (self._key_index + 1) % len(ks)
            return k
        # failover / least_used: 返回第一个（配置顺序）
        return ks[0]

    def _try_keys(self, func, *args, **kwargs):
        """尝试所有 Key，直到成功或耗尽。
        
        - Key 级错误（401/403/429）：尝试下一个 Key
        - Provider 级错误（5xx/timeout）：抛出，由 Router 处理跨商切换
        """
        ks = self.keys
        # 允许无 Key 运行（如 kilogw、测试环境），直接调用一次
        if not ks:
            return func("", *args, **kwargs)

        last_key_error = None
        for i, key in enumerate(ks):
            try:
                return func(key, *args, **kwargs)
            except QuotaExhausted as e:
                # 429 属于 Key 级错误：换下一个 Key 继续。
                # （多账号共享额度的关键；此前这里漏掉 QuotaExhausted，
                #   导致第一个 Key 超额就整体失败，后面的账号白白浪费）
                last_key_error = e
                continue
            except ProviderError as e:
                # Provider 级错误直接抛出，由 Router 路由到下一个提供商
                if e.status_code and e.status_code >= 500:
                    raise
                if e.status_code in (401, 403, 429):
                    last_key_error = e
                    continue  # 尝试下一个 Key
                raise  # 其他状态码直接抛出
            except Exception:
                raise
        # 所有 Key 都试完了
        raise last_key_error or ProviderError("all keys exhausted", status_code=429)

    async def _try_keys_async(self, func, *args, **kwargs):
        """异步版 Key 轮换（真正会生效的那一版）。

        注意：同步的 _try_keys 对 async func 是**无效**的——`func(key)` 只是创建协程、
        并不执行，所以 try/except 永远捕获不到 HTTP 异常，多账号 Key 形同虚设。
        这里 await 之后再捕获，Key 级错误（401/403/429）才会正确换下一个 Key。
        """
        ks = self.keys
        if not ks:
            return await func("", *args, **kwargs)

        last_key_error = None
        for key in ks:
            try:
                return await func(key, *args, **kwargs)
            except QuotaExhausted as e:
                # 429：该 Key 额度用尽 → 换下一个账号继续
                last_key_error = e
                continue
            except ProviderError as e:
                if e.status_code and e.status_code >= 500:
                    raise                      # Provider 级故障，交给 Router 跨商切换
                if e.status_code in (401, 403, 429):
                    last_key_error = e
                    continue                   # Key 无效/受限 → 换下一个
                raise
        raise last_key_error or ProviderError("all keys exhausted", status_code=429)

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

        async def _do_request(key: str):
            async with self._client() as client:
                resp = await client.post(url, json=payload,
                                         headers=self._headers(body, key))
            if resp.status_code == 429:
                raise QuotaExhausted(f"rate limited: {resp.text[:200]}")
            if resp.status_code != 200:
                raise ProviderError(resp.text[:300], status_code=resp.status_code)
            data = resp.json()
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

        return await self._try_keys_async(_do_request)

    # ---------- 流式 ----------
    async def _open_stream(self, url, payload):
        """建立流式连接：状态错误在此抛出（供 Router 回退），成功后返回生成器。"""
        
        async def _do_stream(key: str):
            client = self._client()
            try:
                req = client.build_request("POST", url, json=payload,
                                           headers=self._headers(payload, key))
                resp = await client.send(req, stream=True)
            except httpx.TimeoutException:
                await client.aclose()
                raise ProviderError("timeout", status_code=504, retry_after=30)
            except httpx.ConnectError as e:
                await client.aclose()
                raise ProviderError(f"connect error: {e}", status_code=503)
            except Exception as e:
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

        return await self._try_keys_async(_do_stream)

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
    from . import amd, modelscope, kilogw, zen
    pool = {}
    for name, pcfg in cfg.providers.items():
        if name == "amd-radeon":
            pool[name] = amd.AMDAdapter(pcfg)
        elif name == "modelscope":
            pool[name] = modelscope.ModelScopeAdapter(pcfg)
        elif name == "kilogw":
            pool[name] = kilogw.KiloProvider(pcfg)
        elif name == "zen":
            pool[name] = zen.ZenFreeAdapter(pcfg)
        else:
            # 未知商统一走 OpenAI 兼容适配器（含新增 7 家国内免费商）
            pool[name] = OpenAICompatAdapter(pcfg)
    return pool