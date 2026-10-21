# Free-Router 使用说明

免费模型聚合与路由系统 —— 让 **Hermes / QwenPAW / Harness** 三个工具共用一套统一模型接入，自动跨提供商路由、容错、识别图像，无需关心底层免费模型是谁。

---

## 一、系统能干什么

1. **一个端点接入**：三个工具都指向本系统的 OpenAI 兼容端点 `/v1/chat/completions`，底层免费模型由系统自动调度。
2. **自动路由**：按能力分 + 额度状态，自动选当前最优免费模型。
3. **跨商容错**：某家免费额度用完/报错，自动切到下一家（最多 20 次重试）。
4. **自动识别图像**：消息含图时，自动选用视觉模型（如 `DeepSeek-V4-Flash-Vision-Exp`）。
5. **额度监控**：RPM/RPD/TPM/TPD 四维追踪 + 分级冷却（429 递增、5xx 短冷却、密钥不健康不重试）。
6. **模型生命周期**：每 12h 自动发现新模型/下线模型/改名/免费转付费切换。
7. **会话粘性**：30 分钟内多轮对话保持同一模型，避免切换打断。

## 二、当前提供商池（3 家，已移除本地模型）

| 提供商 | 接入地址 | 需 key? | 视觉模型 | 文本模型 |
|--------|----------|---------|----------|----------|
| **AMD Radeon** | `developer.amd.com.cn/radeon/api/v1` | ✅ rc-xxx | `DeepSeek-V4-Flash-Vision-Exp`、`Qwen2.5-VL-72B` | `DeepSeek-V4-Flash`、`Qwen3.8-Flash-Next` |
| **ModelScope 魔塔** | `api-inference.modelscope.cn/v1` | ✅ 各家独立 | `OpenGVLab/InternVL3_5-241B-A28B` | `Qwen/Qwen3.8-Flash-Next` |
| **Kilo 网关** | `api.kilo.ai/api/openrouter` | ❌ 免 key | `inclusionai/ling-3.0-flash-vl:free` | 各家 free 模型 |

> 如需新增提供商：在 `config/config.yaml` 的 `providers` 下加一段即可，无需改代码。

## 三、API Key 填写位置（重点）

**原则**：需要 key 的提供商已留出 `api_key` 字段（即"可自定义填 key 的地址栏"）。当前留空，填上即可用。

### 三种填法（优先级从高到低）
1. **`api_key` 字段直接写** —— 推荐，最直观
2. **`key_env` 环境变量** —— 从系统环境变量读
3. **留空** —— 仅适用于免 key 提供商（Kilo）

### 具体位置：`config/config.yaml`
```yaml
providers:
  amd-radeon:
    api_key: ""      # ← 在这里填 AMD 的 rc-xxx 密钥
  modelscope:
    api_key: ""      # ← 在这里填 ModelScope 密钥（三家工具各填各的）
```

### ModelScope 三家工具独立 key 说明
- **Hermes**：用 Hermes 自己 config 里的 `auxiliary.vision`（独立一份）
- **QwenPAW**：在 QwenPAW 的 provider/ACP 配置里填（独立一份）
- **Harness**：在 Harness 运行配置里填（独立一份）
- 三份 key 互不共享，各自独立。

## 四、安装与启动

```bash
cd free-router

# 1. 装依赖
pip install -r requirements.txt

# 2. （可选）填 key
#    编辑 config/config.yaml 里的 api_key 字段，或创建 config/.env：
#    AMD_API_KEY=rc-xxx
#    MODELSCOPE_API_KEY=sk-xxx

# 3. 启动服务
python run.py
#   或
python -m uvicorn app.main:app --app-dir . --host 127.0.0.1 --port 8787
```

启动后服务监听 `http://127.0.0.1:8787`

## 五、三个工具如何接入

### 通用：都指向统一端点
```
base_url = http://127.0.0.1:8787/v1
model    = auto   （或用任意池内模型 ID，如 DeepSeek-V4-Flash）
```

### Hermes
在 `<本机路径>` 配置 provider 指向本服务，或直接复用其自带 vision 配置（AMD + DeepSeek-V4-Flash-Vision-Exp，已实测可用）。

### QwenPAW / Harness
在其对应的 provider / ACP 配置项填入上面的 `base_url + model`，并各自填自己的 ModelScope key。

## 六、Skill 命令（CLI）

```bash
# 查看模型/额度/冷却状态
python skill/scripts/fr.py status

# 触发模型自动发现刷新
python skill/scripts/fr.py refresh

# 强制切换到某模型
python skill/scripts/fr.py switch amd-radeon/DeepSeek-V4-Flash

# 手动冷却某模型
python skill/scripts/fr.py cooldown modelscope <model> 429

# 启动额度守护进程（每 60s 探测）
python skill/scripts/watcher.py 60
```

## 七、HTTP 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | 主聊天端点（OpenAI 兼容） |
| GET | `/v1/models` | 列出可用模型 |
| GET | `/health` | 健康检查 |
| GET | `/admin/status` | 模型/额度/冷却快照 |
| POST | `/admin/refresh` | 手动触发模型发现 |
| POST | `/admin/switch` | 强制切换模型 |
| POST | `/admin/cooldown` | 手动冷却 |
| POST | `/admin/reset_cooldowns` | 清空所有冷却 |

## 八、已验证功能

- ✅ 跨提供商自动切换（AMD 失败 → ModelScope）
- ✅ 视觉模型自动选择（含图消息走视觉模型）
- ✅ RPM/RPD/TPM/TPD 四维额度记录
- ✅ 429 冷却递增（60s → 5min → 30min）
- ✅ model_not_found 自动下线
- ✅ 30 分钟会话粘性
- ✅ 模型自动发现 + 下线
- ✅ 免费转付费 slug 解析
- ✅ 服务启动 /health、/v1/models（40 模型）

## 九、常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 请求报 503 all_free_models_exhausted | 所有免费模型额度耗尽/不可用 | `fr status` 看冷却，稍后重试；或填新 key |
| 报 401 key_unhealthy | api_key/key_env 未填或错 | 在 config 填正确 key |
| 某家一直失败 | 该家额度用完 | 系统自动切走，无需干预 |
| 想用某具体模型 | 强制指定 | `fr switch provider/model` |
| 模型不识别图像 | 选的模型无视觉能力 | 自动路由会换视觉模型 |

## 十、目录结构

```
free-router/
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── core/            # config/registry/quota/router/lifecycle
│   └── providers/       # amd / modelscope / kilogw 适配器
├── config/
│   ├── config.yaml      # 主配置（含 api_key 槽位）
│   └── .env.example     # 环境变量示例
├── skill/
│   ├── SKILL.md         # Hermes Skill 定义
│   └── scripts/         # fr.py CLI + watcher.py 守护
├── tests/               # 单元测试
├── data/                # SQLite 数据
├── README.md            # 架构方案文档
├── USAGE.md             # 本使用说明
├── requirements.txt
└── run.py
```

## 十一、后续规划（待做）

- 动态能力评分（MMLU/HumanEval 基准加权）
- 学习型路由（根据历史成功率调整）
- 更多提供商适配
- Agent 深度集成（delegate_task 动态指定模型）