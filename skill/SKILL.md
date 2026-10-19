"""Free-Router Skill 定义
免费模型聚合与路由系统——跨提供商免费模型自动路由/切换。
"""
description: |
  Free-Router：聚合 AMD Radeon/ModelScope/Kilo网关 等免费提供商，
  自动按能力与额度路由，跨商切换最多20次重试，做成 Hermes Skill。
  用于：需要免费/低成本模型、跨提供商自动容错、识图(视觉)、额度监控。
  
  参考实现：open-free-router / FreeLLMAPI / FreeRide-H。

当用户需要：免费模型聚合路由、自动切换提供商、识别图像、规避额度限制、
或多个工具(Agent/QwenPAW/Harness)共用统一模型接入点时，加载本技能。

# 配置位置
- 配置文件: L:/free-router/config/config.yaml
- 密钥:     L:/free-router/config/.env  (AMD_API_KEY / MODELSCOPE_API_KEY)
- 数据库:   L:/free-router/data/free_router.db

# 使用前
1. 确认依赖已装: `pip install fastapi uvicorn httpx pyyaml`
2. 启动服务: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8787`
3. 健康检查: `curl http://127.0.0.1:8787/health`

# CLI 命令 (skill/scripts/fr.py)
- fr status          # 查看模型/额度/冷却快照
- fr refresh         # 触发模型自动发现
- fr switch <model>  # 强制切换到指定模型
- fr switch <provider>/<model>  # 指定提供商+模型
- fr cooldown <p> <m> <reason>  # 手动冷却

# 与其他工具共用
- Hermes:  config.yaml auxiliary.vision 指向本服务端点(或直接指各提供商)
- QwenPAW: 其 ACP/provider 配置填入 base_url + key + model
- Harness: 运行配置同样填入统一端点

# 三工具 key 规则
- AMD_API_KEY / MODELSCOPE_API_KEY 各工具独立配置
- 配置里 api_key 直接写 > key_env 环境变量 > 空(免key)

# 协议
- 应用侧只调 OpenAI 兼容端点 /v1/chat/completions，无需关心底层提供商

## 免费/收费选型 + 任务感知（v0.3）
- 请求可声明任务上下文（三选一/可叠加）：`body.free_router` 结构、`body.user`（JSON 字符串）、请求头
  `X-Free-Router-Task-Type` / `-Tags` / `-Model-Type` / `-Prefer-Free` / `-Tenant`。
- 默认**免费优先**；收费模型只在「显式指定 model」或「prefer_free=false」时参与，且响应头会带
  `X-Free-Router-Model-Type: paid` 与 `X-Free-Router-Notice` 提醒文案（也可读响应体 `_free_router`）。
- `vision` 是**硬能力**：声明后非视觉模型直接排除；若视觉候选全部不可用，则不中断任务、退回最优模型并如实提示。
- 会话粘性不得绕过硬能力与免费策略；只有显式指定与 `/admin/switch` 可越权。

## 运行期增删（不重启）
- `POST/DELETE /admin/models`、`POST/DELETE /admin/providers`；改动落在 `config/providers.local.yaml`（覆盖层），
  删除写**墓碑**，自动发现也不会复活；重新 POST 同一模型即清除墓碑。
- 查看：`GET /admin/models`（池 + 墓碑）、`GET /admin/status`（额度/冷却/预测/缓存/告警/策略）。

## 观测与运维接口
`GET /metrics`（Prometheus 文本，标准库实现）、`GET /admin/cache`、`GET/POST /admin/policy`（热更换打分策略）、
`GET /panel/`（简易面板，含任务上下文测试表单）。

## 重启服务（Windows 必读）
- **不要用 `taskkill //F //PID x`**：MSYS/Git-Bash 下会报"无效参数"，进程杀不掉（表现为改了代码却不生效）。
  可靠做法：`powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"`，确认端口释放后
  再跑 `cscript //nologo scripts/autostart.vbs`。
- 判断新代码是否生效：`curl -s 127.0.0.1:8787/health` 应包含 `"models": {...}` 字段（v0.3 起）。
- 单元测试：`python -m pytest -q`（76 项）。
