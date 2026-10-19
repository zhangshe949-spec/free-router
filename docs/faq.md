# FAQ

## 某提供商一直不被使用

未配 key（自动跳过）；或 429/5xx 冷却中（`GET /admin/status` 看 `cooldowns`）。

## AI 回「额度已用尽」

免费日额度耗尽，属正常；隔天自动恢复，或加别的提供商。

## 模型不发现（自动发现没抓到）

- `lifecycle.discovery_urls` 里没配该商。
- `free_only` 提供商只接纳免费档位。
- 被删除的模型有墓碑，自动发现不会复活（`POST /admin/models` 重新加）。

## 缓存不生效

- `cache.serve_hits` 默认 `false`，只在全部模型不可用时兜底；设为 `true` 才命中即返回。

## WebSocket 断线

客户端负责重连；服务端连接关闭时正确清理（`WebSocketDisconnect` 处理）。鉴权失败返回 1008 关闭码。

## 图像任务被非视觉模型回答

所有视觉模型当前不可用（额度/未配 key）→ 响应头 `Notice` 会如实说明。

## 想临时用收费模型

请求里显式指定 `model`，或 `X-Free-Router-Model-Type: paid`；响应头标 `paid` 并给提醒。

## 删除的模型又冒出来

不会：删除写墓碑；要用则重新 `POST /admin/models` 同一模型。

## 401 / 403

- 401：缺/错 Key（配置了 `server.access_key` 或 `auth.api_keys`）。
- 403：Key 无对应 scope/role（如 viewer 访问 `/admin/*`）。

## 谁在用什么

`GET /metrics` 或 `GET /admin/status`。
