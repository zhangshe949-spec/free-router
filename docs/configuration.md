# 配置

主配置 `config/config.yaml`，密钥一律留空，从 `.env` / 环境变量读取。

## 环境变量 / 密钥来源

按优先级：

1. 进程环境变量
2. `./.env`（仓库根目录）
3. `~/.free-router/.env`（多实例共享；可在 `env_files:` 追加）

`key_env` 支持多候选名（`[RADEON_API_KEY, AMD_API_KEY]`），取值时跳过空值。
**没配 key 的提供商会被自动跳过**。

## providers —— 模型池

```yaml
providers:
  kilogw:
    base_url: https://api.kilo.ai/api/openrouter
    key_env: ""                            # 免 key；需要 key 时填环境变量名
    extra_headers: { Authorization: '' }
    request_timeout: 10
    tier_quality: high
    free_only: true                        # 只接纳免费档位
    type: free                             # free | paid
    models:
      - id: nvidia/nemotron-3-super-120b-a12b:free
        capability: {score: 80, code: 78, reason: 80, long: 70, vision: false}
        tier: high
        type: free
        tags: [reasoning, code, long]
        priority: 0
        context_window: 131072             # 上下文窗口（token）
        max_output_tokens: 8192
        pricing_per_1k_tokens: {input: 0.0, output: 0.0}
        supported_parameters: [temperature, top_p, max_tokens, tools]
```

### 模型字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `capability` / `tier` | str / dict / str | 模型标识、能力分、质量档 |
| `type` | `free` \| `paid` | 免费/收费 |
| `tags` | list | 能力标签（reasoning/code/vision/long/...） |
| `priority` | int | 越大越优先（人工置顶） |
| `context_window` | int | 上下文窗口，选择器长文本任务加权用 |
| `max_output_tokens` | int | 单次最大输出 |
| `pricing_per_1k_tokens` | dict | 每千 token 单价 `{input, output}` |
| `supported_parameters` | list | 支持的参数（temperature/top_p/tools/...） |

## 免费 / 收费策略

收费模型与免费模型**同池**，用 `type: paid` 区分。默认（`prefer_free: true`）不会自动选中收费模型；
只有「显式指定 `model`」或「`prefer_free: false`」时才参与。

## 鉴权（auth）

```yaml
server:
  access_key: ""           # 兼容旧版：非空即生成一个 scope=all 的 api_key

auth:
  api_keys:                # 多 Key，每 Key 绑定 tenant/scopes/rbac
    - key: "sk-xxx"
      tenant: default
      scopes: [chat, batch, ws]
      roles: [user]
      rate_limit: {rpm: 60, rpd: 1000}
      models_allowlist: []
      models_denylist: []
  jwt:                     # 可选 JWT
    issuer: ""
    audience: ""
    jwks_url: ""
    algorithm: RS256
  rbac:                    # 角色 → 权限
    admin: ["*"]
    user: ["chat", "batch", "ws"]
    viewer: ["read"]
```

## 可观测性（observability）

```yaml
observability:
  log_level: INFO
  json_logs: true
  otlp_endpoint: ""            # 如 http://jaeger:4317
  otlp_headers: {}
  sampling_ratio: 1.0
  propagators: [tracecontext, baggage]
  metric_export_interval: 60
  console_export: false
```

完整字段见 [可观测性](observability.md)。
