# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""opencode Zen「匿名免费车道」适配器。

背景（2026-10 实测）：https://opencode.ai/zen/v1 对匿名客户端（`Authorization: Bearer public`）
设了一道指纹闸门，下列三条必须**同时**满足才放行，缺一条即返回

    {"type":"error","error":{"type":"FreeTierError",
     "message":"OpenCode's free tier can only be used from within OpenCode."}}

  1. 上游请求必须 `stream=true`（非流式一律拒）；
  2. 必须带结构化头：`x-opencode-session`（`ses_` + 12 位小写 hex 的**反向毫秒时钟** + 14 位 base62）、
     `x-session-affinity`、`X-Session-Id`、`x-opencode-request`（`req_...`）、`x-opencode-project`、
     `x-opencode-client: cli`，且 UA 形如 `opencode/1.18.31 (win32 x64; node...)`；
  3. body.tools 必须像 agent 的工具表（至少含小写的 `bash` / `read`）。

本适配器负责补齐这三件事（指纹工具、会话头、强制流式），并在下游没要流时
把上游 SSE 聚合成一份标准 chat.completion。若模型误调了「指纹诱饵工具」，
这里会把该 tool_call 从流/聚合结果里剔掉，避免客户端收到不认识的工具名。

额度按 session 计费，所以会话 id 用「首条 user 消息摘要」做种子：
同一段对话稳定复用同一 session，不同对话各自开新 session。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time

from ..core.router import ProviderError, QuotaExhausted
from . import BaseAdapter

B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# 车道闸门认的四个「指纹工具」名（全小写）
GATE_TOOLS = ("bash", "glob", "grep", "read")
PUBLIC_KEY = "public"
CLIENT_UA = "opencode/1.18.31 (win32 x64; node22.14.0)"


# ---------------- 会话 / 请求 id ----------------
def _base62(value: int, width: int) -> str:
    out = [""] * width
    v = int(value)
    for i in range(width - 1, -1, -1):
        out[i] = B62[v % 62]
        v //= 62
    return "".join(out)


def _inverted_clock_hex(now_ms: int | None = None) -> str:
    """反向毫秒时钟：车道按此值判断「这是不是一个真客户端的会话」。"""
    current = (int(now_ms if now_ms is not None else time.time() * 1000) * 0x1000) + 1
    inverted = (~current) & 0xFFFFFFFFFFFF
    return format(inverted, "012x")


def _session_id(seed: str) -> str:
    digest = hashlib.sha256(b"ses\x00" + seed.encode("utf-8", "ignore")).digest()
    return f"ses_{_inverted_clock_hex()}{_base62(int.from_bytes(digest[6:16], 'big'), 14)}"


def _request_id() -> str:
    return "req_" + secrets.token_hex(16)


def _project_id() -> str:
    return "prj_" + secrets.token_hex(16)


def _conversation_seed(messages) -> str:
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        encoded = json.dumps(m.get("content"), ensure_ascii=False, sort_keys=True)
        if encoded and encoded != "null":
            return encoded
    return secrets.token_hex(8)


def _tool_name(tool: dict) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"].strip()
    if isinstance(tool.get("name"), str):
        return tool["name"].strip()
    return ""


def _decoy(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Reserved by the host runtime; never call this tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _apply_gates(body: dict) -> tuple[list[str], dict[str, str]]:
    """补齐指纹工具（就地改 body）。

    返回 (诱饵工具名列表, {发出去的规范名 -> 客户端原名})。
    """
    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    client_names = {_tool_name(t) for t in tools if _tool_name(t)}
    out, seen, restore = [], set(), {}
    for tool in tools:
        name = _tool_name(tool)
        low = name.lower()
        if low in GATE_TOOLS:
            if low in seen:
                continue
            seen.add(low)
            if name != low:                       # 客户端写的是 Bash/Read → 规范成小写
                restore[low] = name
                fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
                tool = ({**tool, "function": {**fn, "name": low}} if fn
                        else {**tool, "name": low})
        out.append(tool)

    decoys = []
    for name in GATE_TOOLS:
        if name not in seen:
            out.append(_decoy(name))
            decoys.append(name)

    body["tools"] = out
    if not body.get("tool_choice"):
        body["tool_choice"] = "auto" if client_names else "none"
    body["_client_tool_names"] = client_names      # 传给响应侧过滤（不发给上游）
    return decoys, restore


class ZenFreeAdapter(BaseAdapter):
    """opencode Zen 匿名免费车道（免 key，Bearer public）。"""

    name = "zen"

    def _headers(self, body=None, key: str = None):  # noqa: ARG002
        seed = _conversation_seed((body or {}).get("messages"))
        session = _session_id(seed)
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {PUBLIC_KEY}",
            "User-Agent": CLIENT_UA,
            "x-opencode-client": "cli",
            "x-opencode-session": session,
            "x-session-affinity": session,
            "X-Session-Id": session,
            "x-opencode-request": _request_id(),
            "x-opencode-project": _project_id(),
        }

    def _build_url(self):
        return f"{self.base_url}/chat/completions"

    # ---------------- 对外入口 ----------------
    async def chat(self, body: dict, model: str):
        payload = dict(body)
        payload["model"] = model
        payload["stream"] = True                      # 车道强制流式
        decoys, restore = _apply_gates(payload)
        payload.pop("_client_tool_names", None)
        url = self._build_url()
        want_stream = bool(body.get("stream"))

        if want_stream:
            gen = await self._try_keys_async(self._open_stream, url, payload)
            return self._filter_stream(gen, decoys, restore)

        raw = await self._try_keys_async(self._run_collect, url, payload)
        return self._aggregate(raw, model, decoys, restore)

    # ---------------- 非流式：跑一遍流再聚合 ----------------
    async def _run_collect(self, key: str, url: str, payload: dict):
        async with self._client() as client:
            req = client.build_request("POST", url, json=payload, headers=self._headers(payload, key))
            try:
                resp = await client.send(req, stream=True)
            except Exception as e:                     # noqa: BLE001
                raise ProviderError(f"zen stream connect error: {e}", status_code=502)
            try:
                if resp.status_code == 429:
                    raise QuotaExhausted(f"zen rate limited: {resp.text[:160]}")
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    raise ProviderError(text, status_code=resp.status_code)
                buf = []
                async for line in resp.aiter_lines():
                    if line:
                        buf.append(line)
                return buf
            finally:
                await resp.aclose()

    # ---------------- 流式：建立连接 ----------------
    async def _open_stream(self, key: str, url: str, payload: dict):
        client = self._client()
        try:
            req = client.build_request("POST", url, json=payload, headers=self._headers(payload, key))
            resp = await client.send(req, stream=True)
        except Exception as e:                         # noqa: BLE001
            await client.aclose()
            raise ProviderError(f"zen stream connect error: {e}", status_code=502)

        if resp.status_code == 429:
            await resp.aclose(); await client.aclose()
            raise QuotaExhausted("zen rate limited (stream)")
        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "ignore")[:300]
            await resp.aclose(); await client.aclose()
            raise ProviderError(text, status_code=resp.status_code)

        async def _iter():
            try:
                async for line in resp.aiter_lines():
                    if line:
                        yield line
            finally:
                await resp.aclose()
                await client.aclose()

        return _iter()

    # ---------------- 响应侧：剔除诱饵 tool_call ----------------
    async def _filter_stream(self, lines, decoys, restore):
        dropped: set = set()
        async for line in lines:
            payload = line[5:].strip() if line.startswith("data:") else None
            if payload is None:
                yield line + "\n"
                continue
            if payload in ("", "[DONE]"):
                yield line + "\n"
                continue
            try:
                obj = json.loads(payload)
            except Exception:                          # noqa: BLE001
                yield line + "\n"
                continue
            keep_choices = []
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                tcs = delta.get("tool_calls")
                if tcs:
                    kept = []
                    for tc in tcs:
                        idx = tc.get("index", 0)
                        fn = tc.get("function") or {}
                        nm = fn.get("name")
                        if nm:
                            if nm in decoys:
                                dropped.add(idx)
                                continue
                            if nm in restore:
                                fn["name"] = restore[nm]
                        if idx in dropped:
                            continue
                        kept.append(tc)
                    if kept:
                        delta["tool_calls"] = kept
                    else:
                        delta.pop("tool_calls", None)
                if not delta and ch.get("finish_reason") is None:
                    continue                           # 内容被掏空的空块，不再下发
                keep_choices.append(ch)
            if not keep_choices:
                continue
            obj["choices"] = keep_choices
            yield "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    # ---------------- 聚合 SSE → chat.completion ----------------
    def _aggregate(self, lines, model_req, decoys, restore):
        text, reasoning = [], []
        calls: dict = {}
        dropped: set = set()
        finish = None
        got_model = cid = None
        usage = None

        for line in lines:
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                obj = json.loads(payload)
            except Exception:                          # noqa: BLE001
                continue
            got_model = obj.get("model") or got_model
            cid = obj.get("id") or cid
            usage = obj.get("usage") or usage
            for ch in obj.get("choices") or []:
                finish = ch.get("finish_reason") or finish
                d = ch.get("delta") or {}
                c = d.get("content")
                if isinstance(c, str) and c:
                    text.append(c)
                r = d.get("reasoning_content")
                if isinstance(r, str) and r:
                    reasoning.append(r)
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    nm = fn.get("name")
                    if nm:
                        if nm in decoys:
                            dropped.add(idx)
                            continue
                        if nm in restore:
                            nm = restore[nm]
                    if idx in dropped:
                        continue
                    slot = calls.setdefault(idx, {"id": tc.get("id") or f"call_{idx}",
                                                  "type": "function",
                                                  "function": {"name": nm or "",
                                                               "arguments": ""}})
                    if nm:
                        slot["function"]["name"] = nm
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

        if not text and not calls:
            raise ProviderError("zen: empty upstream stream", status_code=502)

        msg: dict = {"role": "assistant",
                     "content": "".join(text) if text else None}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {
            "id": cid or "chatcmpl-zen",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": got_model or model_req,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }