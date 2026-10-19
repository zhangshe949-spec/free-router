# API 参考

基地址 `http://127.0.0.1:8787`。所有 `/v1/*` 端点 OpenAI 兼容。

## 鉴权

可选。配置 `server.access_key` 或 `auth.api_keys` 后，请求带：

```
Authorization: Bearer <key>
# 或
X-API-Key: <key>
```

无 Key 请求 → `401`；Key 无权限 → `403`（带 `WWW-Authenticate`）。

## POST /v1/chat/completions

```bash
curl -s -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}],"stream":false}'
```

请求体（OpenAI 兼容）可选 `stream`、`model`、`temperature`、`max_tokens` 等；
任务上下文经 `free_router` / `user` / `metadata` 或自定义请求头传递（见 configuration）。

响应（非流式）：

```json
{
  "id": "...",
  "choices": [{"message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 10, "completion_tokens": 20},
  "_free_router": {"provider": "kilogw", "model": "...", "type": "free", "notice": null}
}
```

响应头：`X-Free-Router-Provider` / `-Model` / `-Model-Type` / `-Notice` / `-Cache`。

## POST /v1/messages（Anthropic 兼容）

```bash
curl -s -X POST http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"free-router","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}'
```

## POST /v1/batch/completions（批量，非流式）

```bash
curl -s -X POST http://127.0.0.1:8787/v1/batch/completions \
  -H "Content-Type: application/json" \
  -d '{"requests":[{"messages":[{"role":"user","content":"a"}]},
                   {"messages":[{"role":"user","content":"b"}]}],
       "max_concurrent": 10}'
```

响应：

```json
{"results": [
  {"index": 0, "response": {"choices": [...]}},
  {"index": 1, "error": "all_exhausted: ..."}
]}
```

## GET /v1/ws/chat（WebSocket）

连接后逐条发 JSON；`stream: true` 时把上游 SSE 文本块透传回来，非流式则回一条完整 JSON。

```python
import json, asyncio, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:8787/v1/ws/chat") as ws:
        await ws.send(json.dumps({"messages":[{"role":"user","content":"hi"}], "stream": True}))
        async for m in ws:
            print(m)
asyncio.run(main())
```

## GET /v1/models

模型列表（`expose_single_model: true` 时只暴露聚合别名 `free-router`）。

## GET /metrics

Prometheus 文本指标（需 `metrics.enabled: true`）。

## Admin 端点

需要 `admin` scope/role。前缀 `/admin/*`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/admin/status` | 模型池/额度/冷却/缓存/指标/策略 |
| GET | `/admin/models` | 池中模型 + 墓碑 |
| POST/DELETE | `/admin/models` | 动态增删模型 |
| POST/DELETE | `/admin/providers` | 动态增删提供商 |
| POST | `/admin/refresh` | 重新发现模型 |
| POST | `/admin/switch` | 强制切换模型 |
| GET/POST | `/admin/policy` | 查看/热换打分策略 |
| GET | `/admin/circuit-breakers` | 熔断器状态 |
| GET | `/admin/health-check` | 健康检查状态 |
| GET | `/admin/queue` | 请求队列状态 |
| GET/POST | `/admin/keys` | 提供商 API Key 管理 |

## 错误码

| 状态码 | 含义 |
|---|---|
| 400 | 请求体 JSON 非法 |
| 401 | 未鉴权（缺/错 Key） |
| 403 | 已鉴权但无权限（scope/role 不足） |
| 422 | 参数校验失败 |
| 503 | 全部模型不可用（`all_free_models_exhausted`） |
| 500 | 路由内部错误 |
