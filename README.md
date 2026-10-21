# Free-Router

**一个独立的免费/收费大模型聚合路由网关**（OpenAI 兼容）。把多家免费提供商的模型聚合成一个端点，
按 **质量分级 + 任务需求 + 额度** 自动选型、自动跨提供商顺延、额度恢复自动回切；
全部免费模型都不可用时用本地缓存兜底，保证任务不中断。

- **不依赖任何 agent 框架**：任何能发 HTTP 的智能体（Hermes / QwenPaw / OpenCode / Cursor / 自研脚本）把 `base_url` 指向它即可。
- **零新增第三方依赖**：只用 `fastapi / uvicorn / httpx / pyyaml`（指标、缓存、告警均为标准库实现）。
- **密钥一律留空**：仓库里不写任何 key，全部从 `.env` / 环境变量读取（见下）。

> **版权声明 / Copyright**
> Copyright (c) 2026 [zhangshe949-spec](https://github.com/zhangshe949-spec). **All rights reserved.**
> 本项目为**专有软件（非开源）**：允许个人在本地非商业运行，**禁止**复制再分发、镜像、商用或作为托管服务对外提供。
> 完整条款见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

---

## 1. 快速开始

```bash
# 方式 A：pip 安装（推荐）
pip install .
free-router                       # 默认 127.0.0.1:8787

# 方式 B：直接跑
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

# 方式 C：Docker（零 Python 环境要求）
docker compose up -d
```

验证：

```bash
curl -s http://127.0.0.1:8787/health          # {"status":"ok",...}
curl -s -X POST http://127.0.0.1:8787/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"messages":[{"role":"user","content":"reply OK only"}]}'
curl -s http://127.0.0.1:8787/metrics | head
```

任一端接入（OpenAI SDK / 框架）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="not-needed")
print(client.chat.completions.create(model="", messages=[{"role":"user","content":"hi"}]))
```

---

## 2. 密钥：一律留空

`config.yaml` 里所有 `api_key` 保持 `""`。密钥只在下面三处读取（按优先级）：

1. 进程环境变量
2. `./.env`（仓库根目录，**不要提交**）
3. `~/.free-router/.env`（多实例共享；也可在 `env_files:` 里追加绝对路径）

```bash
cp .env.example .env      # 模板里所有 key 都是空的，按需填写
```

`key_env` 支持多候选名（如 `[RADEON_API_KEY, AMD_API_KEY]`），取值时跳过空值。
**没配 key 的提供商会被自动跳过**（不会浪费一次 failover，也不会留下永久"不健康"冷却）。

---

## 3. 配置说明（`config/config.yaml`）

### 3.1 `providers` —— 模型池（免费/收费同池，用 `type` 区分）

```yaml
providers:
  kilogw:                                  # 提供商名（任意）
    base_url: https://api.kilo.ai/api/openrouter
    key_env: ""                            # 免 key；需要 key 时填环境变量名
    extra_headers: { Authorization: '' }   # 特殊网关的额外头
    request_timeout: 10
    tier_quality: high                     # 该商默认质量档（模型可覆盖）
    free_only: true                        # 只接纳免费档位（:free / kilo-auto/free）
    type: free                             # free | paid（该商默认档位）
    models:
      - id: nvidia/nemotron-3-super-120b-a12b:free
        capability: {score: 80, code: 78, reason: 80, long: 70, vision: false}
        tier: high                         # high | medium | low（高质量优先，low 仅兜底）
        type: free                         # 覆盖提供商默认档位
        tags: [reasoning, code, long]      # 能力标签（任务感知选型用）
        priority: 0                        # 越大越优先（人工置顶）
```

收费模型**放在同一个列表**里，只把 `type` 写成 `paid`：

```yaml
  paid-deepseek:
    base_url: https://api.deepseek.com/v1
    key_env: DEEPSEEK_API_KEY
    api_key: ""                # 留空，从 .env 读
    type: paid
    tier_quality: high
    models:
      - id: deepseek-chat
        capability: {score: 82, code: 84, reason: 82, long: 80, vision: false}
        tier: high
        type: paid
        tags: [reasoning, code]
```

> 收费模型的默认行为：**不会被自动选中**；只有「请求里显式指定 `model`」或「`prefer_free: false`」时才参与，
> 且一旦使用，响应头会明确标出（见 §4）。

### 3.2 `router`

```yaml
router:
  max_retries: 20                 # 单次请求最多尝试多少次（跨提供商顺延）
  sticky_ttl_seconds: 1800        # 会话粘性：30 分钟内保持同一模型
  use_selector: true              # 任务感知选型（关掉则退回"能力分+质量档"排序）
  policy:                         # 可插拔打分策略（留空=内置），见 §9
    module: my_pkg.my_policies
    class: WeightedPolicy
    args: {weight_code: 2.0}
```

### 3.3 `quota`（额度 + 预测）

```yaml
quota:
  rpm_limit: 60 / rpd_limit: 1000 / tpm_limit: 200000 / tpd_limit: 5000000
  cooldown_429: 60               # 429 冷却起步；后续递增 300 → 1800
  cooldown_429_escalate: [300, 1800]
  cooldown_5xx: 30
  predict_window_seconds: 30     # 速率统计窗口
  predict_threshold_seconds: 20  # 预计剩余额度撑不到 20s → 提前避开该模型
```

- **额度隔天自动恢复**（跨天惰性归零），恢复后自动重新参与路由。
- **预测是软过滤**：按近期速率估算"还能撑多久"，低于阈值就先用别的模型（窗口衰减后自动回来，不写冷却）。

### 3.4 `cache` / `metrics` / `alerter`

```yaml
cache:                            # 响应缓存 + 离线兜底
  enabled: true
  ttl_seconds: 86400
  max_entries: 2000
  serve_hits: false               # true=命中直接返回（省额度）；false=只在全部模型不可用时兜底

metrics:
  enabled: true                   # GET /metrics（Prometheus 文本，零依赖）

alerter:                          # 错误率告警（默认关闭）
  enabled: false
  type: webhook                   # webhook | smtp | none
  webhook_url: ""
  error_rate_threshold: 0.3
  window_seconds: 300
  min_interval_seconds: 600
  fallback_to_paid: false         # 告警期临时把收费模型提前
```

---

## 4. 任务感知选型：免费 / 收费 + 任务匹配

在请求里带上**任务上下文**，路由器就会按任务挑最合适的模型；不传则退回默认策略（质量档优先）。

三种传递方式（可叠加，优先级：`free_router` > `metadata` > `user`）：

```jsonc
// 1) 请求体：显式结构（推荐）
{"messages": [...], "free_router": {"task_type": "code_generation",
                                    "required_tags": ["reasoning"],
                                    "prefer_free": true,
                                    "tenant": "team-a"}}

// 2) 请求体：OpenAI 兼容的 user 字段（JSON 字符串或对象都支持）
{"messages": [...], "user": "{\"task_type\":\"image_description\",\"required_tags\":[\"vision\"]}"}

// 3) 请求头
X-Free-Router-Task-Type: code_generation
X-Free-Router-Tags: reasoning,code
X-Free-Router-Model-Type: paid        # 显式要收费模型
X-Free-Router-Prefer-Free: false
X-Free-Router-Tenant: team-a
```

| 键 | 取值 | 含义 |
|---|---|---|
| `task_type` | `code_generation` / `code_review` / `reasoning` / `image_description` / `chat` / `writing` / `summarization` / `translation` | 任务类型，决定"需要什么能力" |
| `required_tags` | `reasoning` `code` `chat` `vision` `long` … 或逗号分隔字符串 | 需要的能力标签 |
| `prefer_free` | `true`（默认）/ `false` | 是否优先免费模型 |
| `model_type` | `free` / `paid` | 显式声明要用哪一类 |
| `tenant` | 任意字符串 | 多租户计量隔离 |

**选型规则（按序）**

1. **过滤**：无对应的 key 的提供商、冷却中的模型、额度耗尽、预计即将耗尽 → 排除。
2. **硬能力**：`required_tags` / 任务类型里属于**硬能力**的标签（当前为 `vision`）不满足 → 直接排除；
   若因此一个候选都不剩，则**不中断任务**，回退到最优可用模型，并在响应头如实提示（见下）。
3. **打分**：`能力分 × 任务权重 + 质量档权重(high 200/medium 100/low 0) + 标签命中(25/个) + 任务命中(30) + 免费亲和(10) + priority×5`。
4. **免费/收费**：`prefer_free: true`（默认）只在免费模型里选；免费池不可用时才用收费模型兜底并**明确提醒**。
   显式指定 `model`（或管理接口强制切换）是用户意图，可以越权使用收费模型。
5. **粘性/强制**：会话粘性与管理面强制切换优先——但**粘性不得绕过硬能力与免费/收费策略**，只有显式指定与强制切换可以。

**响应头（客户端读不到时可读响应体 `_free_router`）**

| 头 | 示例 | 含义 |
|---|---|---|
| `X-Free-Router-Provider` / `-Model` | `kilogw` / `nvidia/...:free` | 实际命中的提供商与模型 |
| `X-Free-Router-Model-Type` | `free` / `paid` | 实际用的是免费还是收费 |
| `X-Free-Router-Notice` | `using PAID model p/m (free pool unavailable...)` | **提醒**：正在用收费模型 / 与声明不一致 / 能力无法满足 |
| `X-Free-Router-Cache` | `HIT` / `STALE-FALLBACK` | 该响应来自缓存 |

常见提醒文案：

- `using PAID model <p/m> (free pool unavailable or paid model explicitly requested)` — 正在使用收费模型
- `prefer_free=false but the model actually used is FREE <p/m>` — 想用收费但实际是免费（池里没有更高分的收费模型）
- `no available model satisfies required capabilities ['vision'] (candidates cooling or unconfigured); using the best available model instead` — 声明的能力当前无人满足（如实告知，不改需求）

---

## 5. 运行期增删模型/提供商（无需重启）

改动落在独立覆盖层 `config/providers.local.yaml`（`FREE_ROUTER_OVERLAY` 可改路径），**不动 `config.yaml`**，
重启后自动合并；被删除的模型会写入**墓碑**，连自动发现也不会再把它加回来。

```bash
# 新增模型（可顺带新建提供商；密钥仍从环境变量/.env 读，不写文件）
curl -X POST http://127.0.0.1:8787/admin/models -H "Content-Type: application/json" \
  -d '{"provider":"kilogw","model":"vendor/new-model:free","type":"free",
       "tags":["reasoning","chat"],"tier":"high","priority":1}'

# 下线模型（写墓碑；显式重新 POST 同一模型即自动清除墓碑）
curl -X DELETE http://127.0.0.1:8787/admin/models -H "Content-Type: application/json" \
  -d '{"provider":"kilogw","model":"vendor/new-model:free"}'

# 新增 / 删除提供商
curl -X POST   http://127.0.0.1:8787/admin/providers -d '{"name":"my-gw","base_url":"https://example.com/v1","type":"free","key_env":"MY_GW_KEY"}'
curl -X DELETE http://127.0.0.1:8787/admin/providers -d '{"name":"my-gw"}'

# 查看池与墓碑
curl -s http://127.0.0.1:8787/admin/models
```

覆盖层结构：

```yaml
providers:
  my-gw:
    base_url: https://example.com/v1
    type: free
    key_env: MY_GW_KEY
    models:
      - {id: some-model:free, tags: [chat], tier: medium, priority: 0}
disabled:                       # 墓碑：从静态清单/自动发现里剔除
  - {provider: kilogw, model: vendor/old-model:free}
```

---

## 6. 多租户

`tenant` 维度贯穿用量与冷却（默认 `"default"`；旧库首次启动自动迁移，原数据归入 `default`）。
同一租户的额度耗尽不会影响其他租户。

```bash
curl -X POST http://127.0.0.1:8787/v1/chat/completions -H "X-Free-Router-Tenant: team-a" ...
curl -s http://127.0.0.1:8787/admin/status     # quota.usage 里按 "team-a:provider/model" 分面
```

---

## 7. 管理接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 存活 + 提供商 + 模型统计 |
| GET | `/v1/models` | 模型列表（含 `type` / `tier` / `tags`） |
| GET | `/metrics` | Prometheus 指标（请求/延迟/额度/顺延/缓存/在线时长） |
| GET | `/admin/status` | 模型池、额度、冷却、预测耗尽、缓存、指标、告警、策略、配置 |
| GET | `/admin/models` | 池中模型 + 墓碑清单 |
| POST/DELETE | `/admin/models` | 动态增删模型 |
| POST/DELETE | `/admin/providers` | 动态增删提供商 |
| POST | `/admin/refresh` | 立即重新发现各提供商模型 |
| POST | `/admin/switch` | 强制切换模型（`{model, provider}` 或 `model="provider/model"`） |
| POST | `/admin/cooldown` | 手动冷却某模型；`/admin/reset_cooldowns` 全部解除 |
| GET/POST | `/admin/policy` | 查看/热更换打分策略（见 §9） |
| GET/POST | `/admin/cache` | 缓存统计；`action=purge` 清理过期 |
| GET | `/panel/` | 简易 Web 面板（状态 + 任务上下文测试表单） |

---

## 8. 离线兜底与告警

- **缓存兜底（默认开启）**：全部模型不可用时，用同一请求最近一次成功响应返回，头标 `X-Free-Router-Cache: STALE-FALLBACK`；
  `serve_hits: true` 时缓存命中直接返回（省额度，可能返回过期内容）。
- **告警（默认关闭）**：近 `window_seconds` 内错误率超 `error_rate_threshold` → webhook/SMTP 通知；
  `fallback_to_paid: true` 可在告警期临时把 `type: paid` 模型提前。告警失败不影响主流程。

---

## 9. 可插拔打分策略

内置策略见 `app/core/selector.py`。自定义只需实现 `score(entry, ctx, base) -> float`：

```python
# my_pkg/my_policies.py
class WeightedPolicy:
    def __init__(self, weight_code=2.0): self.w = weight_code
    def score(self, entry, ctx, base):
        s = base
        if "code" in (entry.tags or []) and ctx.get("task_type") == "code_generation":
            s += 50 * self.w
        if entry.type == "paid":          # 收费模型适度降权（可配置）
            s -= 30
        return s
```

配置或热加载：

```yaml
router:
  policy: {module: my_pkg.my_policies, class: WeightedPolicy, args: {weight_code: 2.0}}
```

```bash
curl -X POST http://127.0.0.1:8787/admin/policy -d '{"module":"my_pkg.my_policies","class":"WeightedPolicy"}'
```

策略抛异常时自动退回内置算法，不会中断任务。

---

## 10. 测试与自检

```bash
python -m pytest -q                 # 76 项（路由/额度/多租户/预测/缓存/指标/策略/动态增删/协议转换）
python scripts/probe_pool.py        # 逐模型真实探活，输出 data/probe_report.json
```

Windows 上的常驻与自愈见 `skill/`（登录自启脚本 + 看门狗；脚本自定位，可放任意路径）：

- `skill/scripts/run_server.py` —— pythonw 无控制台启动（**必须先把 stdout/stderr 重定向到日志**，否则 uvicorn 日志会崩）
- `skill/scripts/watchdog.py` —— 每 60s 探活，掉线自动拉起
- `skill/scripts/autostart.vbs` / `watchdog.vbs` —— HKCU Run 注册用

> Windows 提示：MSYS/Git-Bash 下 `taskkill //F //PID x` 会报"无效参数"，用
> `powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"` 才可靠。

---

## 11. 目录结构

```
app/
  main.py             FastAPI 入口 + 管理面
  cli.py              free-router 命令行入口
  anthropic.py        Anthropic /v1/messages 协议转换
  core/
    config.py         配置加载（含覆盖层合并、密钥解析）
    registry.py       模型注册表（SQLite 持久化、墓碑）
    router.py         过滤 → 选型 → 重试 → 跨商切换
    selector.py       任务感知打分（免费/收费、标签、硬能力、提醒）
    policy.py         可插拔策略加载
    quota.py          额度/冷却/多租户/预测
    cache.py          响应缓存与离线兜底
    metrics.py        Prometheus 指标（标准库）
    alerter.py        错误率告警
    overlay.py        运行期覆盖层（动态增删持久化）
    lifecycle.py      自动发现/下线
  providers/          OpenAI 兼容适配器
config/config.yaml    主配置（密钥一律留空）
config/providers.local.yaml   运行期覆盖层（自动生成）
static/               面板
tests/                76 项单测
scripts/probe_pool.py 模型探活
skill/                Windows 常驻/自愈脚本
```

---

## 12. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 某提供商一直不被使用 | 未配 key（自动跳过）；或 429/5xx 冷却中（`/admin/status` 看 `cooldowns`） |
| AI 回"额度已用尽" | 免费日额度耗尽，属正常；隔天自动恢复，或加别的提供商 |
| 图像任务被非视觉模型回答 | 所有视觉模型当前不可用（额度/未配 key）→ 响应头 `Notice` 会如实说明 |
| 想临时改用收费模型 | 请求里显式指定 `model`，或 `X-Free-Router-Model-Type: paid`；响应头会标 `paid` 并给提醒 |
| 删除的模型又冒出来 | 不会：删除写墓碑；若要用，重新 POST 同一模型即可清除墓碑 |
| 想看谁在用什么 | `GET /metrics` 或 `GET /admin/status` |