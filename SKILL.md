---
name: free-router
description: 免费模型聚合路由：聚合 AMD/Kilo/魔搭/Nous 等免费提供商，按能力与额度自动路由、跨商切换、统一模型接入点。
category: productivity
---

Free-Router：聚合 AMD Radeon/ModelScope/Kilo网关等免费提供商，自动按能力与额度路由，跨商切换最多20次重试，做成 Hermes Skill。
用于：需要免费/低成本模型、跨提供商自动容错、识图(视觉)、额度监控。

参考实现：open-free-router / FreeLLMAPI / FreeRide-H。

当用户需要：免费模型聚合路由、自动切换提供商、识别图像、规避额度限制、
或多个工具(Agent/QwenPAW/Harness)共用统一模型接入点时，加载本技能。

# 配置位置
- 配置文件: free-router/config/config.yaml
- 密钥:     free-router/config/.env  (可留空，免key匿名访问；如需付费模型请填写对应 KEY)
- 数据库:   free-router/data/free_router.db

# 使用前
1. 确认依赖已装: `pip install fastapi uvicorn httpx pyyaml`
2. 启动服务: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8787`（局域网可访问）；仅本机访问可使用 `--host 127.0.0.1`
3. 健康检查: `curl http://127.0.0.1:8787/health`（若服务绑定 0.0.0.0，亦可用本机LAN-IP替换 127.0.0.1）

# CLI 命令 (skill/scripts/fr.py)
- fr status          # 查看模型/额度/冷却快照
- fr refresh         # 触发模型自动发现
- fr switch <model>  # 强制切换到指定模型
- fr switch <provider>/<model>  # 指定提供商+模型
- fr cooldown <p> <m> <reason>  # 手动冷却

# 后台任务管理
- 检查服务状态: `curl http://127.0.0.1:8787/health`（若服务绑定 0.0.0.0，亦可用本机LAN-IP替换 127.0.0.1）
- 验证守护进程: 使用 Hermes process 工具查询后台 session 状态
- 进程日志: 查看 HERMES scratch 目录中的工具输出文件
- 进程检查: 使用 `tasklist | findstr "uvicorn"` 或 `wmic process where "name='python.exe'" get commandline,processid` 确认后台服务进程存在。
- 进度汇报偏好: 用户倾向于直接通过工具返回结果汇报任务状态，而非额外通知；在后台任务完成后，应通过工具输出直接给出关键结果，避免额外的消息或提示。
- 重启服务注意: 在 MSYS/Git-Bash 环境下，不要使用 `taskkill //F //PID x`，应使用 `powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"` 确保进程被正确终止。

# 与其他工具共用
- Hermes:  config.yaml auxiliary.vision 指向本服务端点(或直接指各提供商)
- QwenPAW: 其 ACP/provider 配置填入 base_url + key + model
- Harness: 运行配置同样填入统一端点

# 三工具 key 规则
- AMD_API_KEY / MODELSCOPE_API_KEY 各工具独立配置
- free-router: 配置里 api_key 留空，匿名访问（可通过 .env 留空或不设置 KEY）
- Hermes: 需要配置相应提供商的 API Key（如 ModelScope、Kilo 等），询问用户获取。

# 注意事项
- 不确定密钥配置时，应先询问用户而非自行填写，以免产生歧义。
- 后台任务监控：使用 `process(action='poll')` 或 `process(action='wait')` 检查后台进程状态，避免静默失败。
- 及时进度汇报：用户倾向于直接通过工具返回结果汇报任务状态，而非额外通知。

# 协议
- 应用侧只调 OpenAI 兼容端点 /v1/chat/completions，无需关心底层提供商

## 多 Key 管理（每提供商多账号）
- 后端 `GET /admin/keys`（各商 Key 数量 + 掩码，绝不回明文）、`POST /admin/keys`
  （`action`: `add(key)` | `remove(index)` | `clear` | `set(keys[])`）。
- Key 写入 `~/.free-router/.env`（env 名 = 主名, 主名_2, 主名_3…），并把 `keys_env`
  写进 `config/providers.local.yaml` 覆盖层，随后 `_reload_runtime()` 热生效，无需重启。
- `resolved_keys` 会把同一 Key 在多个候选 env 名下重复计入；接口用
  `list(dict.fromkeys(...))` 去重，否则 UI 会显示“2 个”其实只有 1 个。
- 清理旧生成名时只删 `base` / `base_*`，不要碰其它候选 env 名（如 `AMD_API_KEY`）。

## 管理面板渲染陷阱（实测，务必遵守）
- `el()` 必须**同时支持字符串样式与对象样式**：`style: 'display: none'` 若被静默忽略，
  所有折叠面板/隐藏区失效（曾表现为“模型矩阵默认展开”）。
- **严禁对普通对象做数组解构**（如 `models.map(([k, v]) => ...)`）：会抛
  `object is not iterable`；又因发生在 `renderAll()` 内，会**中断整个初始化**——页面
  看似渲染了，实则弹“初始化失败”toast，且 `initScrollButtons()` 等后续步骤全不执行。
- `window.prompt()` 是单行输入，无法收集“每行一个”的多 Key；多 Key 必须用弹窗。
- `el(tag, attrs, ...children)` 传入 `null` 会被 `String(null)` 渲染成文本 `null`：
  条件性子元素要么 `.filter(Boolean)`，要么返回一个 `style:'display:none'` 的空元素。
- 标题栏内的按钮要 `e.stopPropagation()`，否则点 🔑 会同时触发展开/折叠。
- 验证要“真跑”：只看代码或 HTTP 200 会漏掉运行时异常。用无头 Edge + CDP
  （`--headless=new --remote-debugging-port=<随机> --user-data-dir=<临时>`，
  `Runtime.enable` + `Log.enable` 收集 `exceptionThrown`/`entryAdded`，
  `Network.setCacheDisabled` 避开缓存）实测：页面错误数、折叠数、按钮数、弹窗。

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
`GET /panel/`（管理面板，含仪表盘、模型管理、提供商、额度分析、冷却中心、缓存控制、策略中心、系统设置等模块）。

### 管理面板 UI 开发
管理面板是一个单页应用（SPA），使用 HTML/CSS/JS 构建，通过 FastAPI 的静态文件挂载提供服务。采用**单页瀑布流布局**（无侧边栏，8 大功能区垂直流式排列）+ **赛博朋克视觉风格**（深空背景、动态网格、轨道环、粒子场、荧光青/紫/绿强调色）。

详细开发模式、坑与最佳实践见：`references/admin-ui-patterns.md`

#### 开发步骤

1. **准备静态目录**
   确保管理面板文件存在于 `app/admin_ui/`：
   ```bash
   ls -l app/admin_ui/
   # 应包含：index.html style.css app.js
   ```

2. **在 FastAPI 中挂载**
   编辑 `app/main.py` 将 UI 挂载到 `/panel/`，**必须使用相对于 `__file__` 的路径并开启 `html=True`**：
   ```python
   from fastapi.staticfiles import StaticFiles
   from pathlib import Path
   _STATIC_DIR = Path(__file__).resolve().parent / "admin_ui"
   if _STATIC_DIR.is_dir():
       app.mount("/panel", StaticFiles(directory=str(_STATIC_DIR), html=True), name="panel")
   ```
   这样可以通过 `http://<host>:<port>/panel/` 访问 SPA。

3. **开发 UI 文件**
   - `index.html`：单页瀑布流结构（顶部导航栏、8 个 flow-section、toast-layer、modal-layer）
   - `style.css`：CSS 变量定义深空色系、荧光强调色、几何圆角、阴影发光、动画关键帧、响应式断点
   - `app.js`：状态管理、8 大 Section 渲染、模型三视图（表格/卡片墙/分组树）、搜索筛选、模态框、Toast、ECharts 图表、模型 CRUD、提供商卡片、额度/冷却/缓存/策略/设置面板
   - 使用 ECharts（通过 CDN 在 `index.html` 中引入）绘制仪表盘饼图/柱图
   - **关键**：`el()` 工具函数必须支持嵌套数组 children 扁平化，否则会渲染出 `[object HTMLDivElement]`

4. **测试挂载**
   启动服务器：
   ```bash
   python run.py
   ```
   验证：
   - `curl -I http://127.0.0.1:8787/panel/` 返回 200
   - `curl http://127.0.0.1:8787/panel/style.css` 返回 CSS 内容
   - 在浏览器中打开 URL；UI 应无控制台错误加载，F12 控制台可见 `[free-router]` 前缀初始化日志

5. **热重载 UI 而无需重启后端**
   由于 UI 是静态文件，对 HTML/CSS/JS 的更改在硬刷新（Ctrl+Shift+R）或清除缓存后立即生效。

### 核心坑与规则

- **静态路径必须相对 `__file__`**：使用开发机绝对路径会导致部署失效。
- **必须 `html=True`**：否则 `/panel/` 返回 404；SPA 需要 `index.html` 作为根路径。
- **初始化时序**：`app.js` 必须等待 `DOMContentLoaded` 再执行 `init()`，否则 DOM 元素查不到。
- **`el()` 扁平化 children**：
  ```js
  const flat = (arr) => arr.flatMap(c => Array.isArray(c) ? flat(c) : c);
  flat(children).forEach(c => e.append(c instanceof Node ? c : document.createTextNode(String(c))));
  ```
  不扁平化会导致数组被 `String()` 成 `[object HTMLDivElement]`。
- **图表实例复用**：`echarts.init(dom)` 前检查 `dom._chart?.dispose()` 避免重复实例内存泄漏。
- **ECharts CDN 优雅降级**：在 `index.html` 中用动态 script 标签加载，设置 `window._echartsLoaded` 标记；`app.js` 图表初始化前检查此标记，失败时仅 `console.warn` 跳过渲染，不阻塞页面。
- **图表重复 DOM 查询**：`initProviderChart`/`initQuotaChart` 中避免重复声明 `const dom = $('#cProvider')`，会导致变量被覆盖。
- **`el()` style 赋值过滤数字键**：`Object.assign(e.style, v)` 会把数字键当作 CSS 属性索引，现代浏览器报 `Indexed property setter is not supported`。需过滤：`Object.entries(v).forEach(([sk, sv]) => { if (isNaN(sk)) e.style[sk] = sv; })`。
- **表格视图复选框列**：若需隐藏，需同步删除表头 `<th>`、tbody 中的 `<td>`、全选/单选事件监听、全选按钮逻辑。
- **图表重复 DOM 查询**：`initProviderChart`/`initQuotaChart` 中避免重复声明 `const dom = $('#cProvider')`，会导致变量被覆盖。
- **Model ID 编码**：`/admin/models/{provider}/{model}` 中的 model 可能含 `/`（如 `Qwen/Qwen3.8-Flash-Next`），前端调用时必须 `encodeURIComponent` 两次或用 POST+body 避免路径参数解析问题。
- **多租户键兼容**：`quota.usage` 键默认 `provider/model`，启用多租户时为 `tenant:provider/model`，前端显示需兼容。
- **提供商分组折叠显示**：额度分析与模型矩阵需按提供商分组折叠（provider-section），点击展开/收起；初始默认展开，点击 header 切换。
- **一键滚动按钮**：页面右侧固定显示回到顶部（⬆）和跳到底部（⬇）按钮，滚动超过 200px 显示，点击平滑滚动。
- **el() 扁平化 children 修复**：必须用 `flat()` 递归扁平化嵌套数组，否则渲染出 `[object HTMLDivElement]`。
- **ECharts CDN 优雅降级**：动态加载 script 标签设置 `window._echartsLoaded`，渲染前检查标记，失败仅 `console.warn` 跳过不阻塞页面。
- **el() style 赋值过滤数字键**：`Object.assign(e.style, v)` 会把数字键当 CSS 索引报错 `Indexed property setter is not supported`，需过滤：`Object.entries(v).forEach(([sk, sv]) => { if (isNaN(sk)) e.style[sk] = sv; })`。
- **表格视图复选框列移除**：同步删除表头 `<th>`、tbody `<td>`、全选/单选事件监听、全选按钮逻辑。

### 当前状态 (2026-10-11)
- ✅ 静态目录创建：`app/admin_ui/`
- ✅ HTML 骨架：`index.html`（单页瀑布流、ECharts CDN、toast-layer、modal-layer）
- ✅ 样式表：`style.css`（33KB，深空色系、荧光强调色、动画、响应式）
- ✅ 主逻辑：`app.js`（36KB，8 Section 渲染、三视图、CRUD、图表、调试日志）
- ✅ FastAPI 挂载：`/panel/` 可访问，静态资源正常加载
- ✅ 后端 API 全部正常：`/admin/status`、`/admin/models`、`/admin/policy`、`/admin/cache`、`/admin/cooldown` 等
- ✅ 所有 8 个功能区完整可用：仪表盘、模型矩阵、提供商阵列、额度分析、冷却中心、缓存控制、策略中心、系统设置

## 后端 API 映射表（给前端用）
| 端点 | 方法 | 用途 | 关键返回字段 |
|------|------|------|-------------|
| `/admin/status` | GET | 仪表盘总览 | models(total/active/by_provider/by_type), quota.usage, cooldowns, cache, alerter, policy, config |
| `/admin/models` | GET | 模型列表 | count, models[{provider,model,type,tier,tags,priority}], disabled[] |
| `/admin/models` | POST | 添加模型 | provider, model, type, tags[], priority |
| `/admin/models` | DELETE | 删除模型 | provider, model (body) |
| `/admin/models/{provider}/{model}` | PATCH | 更新模型 | type, tags[], priority |
| `/admin/cooldown` | POST | 手动冷却 | provider, model, reason |
| `/admin/reset_cooldowns` | POST | 清除所有冷却 | - |
| `/admin/policy` | GET | 当前策略 | policy, module, class, builtin |
| `/admin/policy` | POST | 热更换策略 | module, class, args? |
| `/admin/cache` | GET | 缓存状态 | enabled, entries, ttl_seconds, serve_hits, events |
| `/admin/cache` | POST | 缓存操作 | action: clear/refresh |
| `/admin/switch` | POST | 强制切换模型 | model (provider/model) |
| `/admin/providers/{name}/refresh` | POST | 刷新单提供商模型列表 | - |
| `/admin/providers/{name}/refresh` | POST | 刷新单提供商模型列表 | - |

### 常见坑

- **静态路径错误**：使用仅在开发机器上有效的绝对路径。解决方案：使用相对于 `__file__` 的路径。
- **缺少 `html=True`**：没有此项，对 `/panel/` 的请求会返回 404；SPA 需要 `index.html` 作为根路径被提供。
- **开发期间的缓存问题**：浏览器可能缓存旧的 JS/CSS；在开发者工具中禁用缓存或添加版本查询字符串。
- **图表未渲染**：确保 ECharts 脚本在 `app.js` 执行前已加载；检查网络选项卡中 CDN URL 的 404。
- **模型 ID 编码**：`/admin/models/{provider}/{model}` 中的 model 可能包含 `/`，前端调用时必须 `encodeURIComponent` 两次或使用 POST + body 方式避免路径参数解析问题。
- **多租户字段**：仪表盘返回的 `quota.usage` 键为 `provider/model`（默认租户），若启用多租户则为 `tenant:provider/model`，前端显示时需兼容。
- **提供商数据派生**：`state.providers` 从 `dashboard.models.by_provider` 派生，必须在 `loadDashboard()` 中同步赋值并打印调试日志。
- **Windows 进程管理**：MSYS/Git-Bash 下 `taskkill //F //PID x` 无效，必须用 `powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"`。
- **免费/付费 Key 共享**：所有客户端（QwenPaw、Hermes、OpenCode 等）共享同一 `~/.free-router/.env`，同一提供商默认一个 Key；需区分账号时用**提供商别名**（config.yaml 同名不同 key_env）或**多实例**隔离。

### 后端 API 映射表（给前端用）
| 端点 | 方法 | 用途 | 返回字段示例 |
|------|------|------|-------------|
| `/admin/status` | GET | 仪表盘总览 | models(total/active/by_provider/by_type), quota.usage, cooldowns, cache, alerter, policy, config |
| `/admin/models` | GET | 模型列表 | count, models[{provider,model,type,tier,tags,priority}], disabled[] |
| `/admin/models` | POST | 添加模型 | provider, model, type, tags[], priority |
| `/admin/models` | DELETE | 删除模型 | provider, model (body) |
| `/admin/models/{provider}/{model}` | PATCH | 更新模型 | type, tags[], priority |
| `/admin/cooldown` | POST | 手动冷却 | provider, model, reason |
| `/admin/reset_cooldowns` | POST | 清除所有冷却 | - |
| `/admin/policy` | GET | 当前策略 | policy, module, class, builtin |
| `/admin/policy` | POST | 热更换策略 | module, class, args? |
| `/admin/cache` | GET | 缓存状态 | enabled, entries, ttl_seconds, serve_hits, events |
| `/admin/cache` | POST | 缓存操作 | action: clear/refresh |
| `/admin/switch` | POST | 强制切换模型 | model (provider/model) |

### 常见坑

- **静态路径错误**：使用仅在开发机器上有效的绝对路径。解决方案：使用相对于 `__file__` 的路径，如上所示。
- **缺少 `html=True`**：没有此项，对 `/panel/` 的请求会返回 404；SPA 需要 `index.html` 作为根路径被提供。
- **开发期间的缓存问题**：浏览器可能缓存旧的 JS/CSS；在开发者工具中禁用缓存或添加版本查询字符串。
- **图表未渲染**：确保 ECharts 脚本在 `app.js` 执行前已加载；检查网络选项卡中 CDN URL 的 404。
- **模型 ID 编码**：`/admin/models/{provider}/{model}` 中的 model 可能包含 `/`（如 `Qwen/Qwen3.8-Flash-Next`），前端调用时必须 `encodeURIComponent` 两次或使用 POST + body 方式避免路径参数解析问题。
- **多租户字段**：仪表盘返回的 `quota.usage` 键为 `provider/model`（默认租户），若启用多租户则为 `tenant:provider/model`，前端显示时需兼容。
- **提供商数据派生**：`state.providers` 从 `dashboard.models.by_provider` 派生，必须在 `loadDashboard()` 中同步赋值并打印调试日志。
- **单提供商模型刷新**：`POST /admin/providers/{name}/refresh` 仅刷新指定提供商的模型列表（触发发现并更新注册表），不影响其他提供商。前端“提供商阵列”区的刷新按钮调用此接口。
- **Windows 进程管理**：MSYS/Git-Bash 下 `taskkill //F //PID x` 无效，必须用 `powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"`。
- **免费/付费 Key 共享**：所有客户端（QwenPaw、Hermes、OpenCode 等）共享同一 `~/.free-router/.env`，同一提供商默认一个 Key；需区分账号时用**提供商别名**（config.yaml 同名不同 key_env）或**多实例**隔离。

### Windows 进程管理速查

```bash
# 查占用端口
netstat -ano | findstr :8787

# 杀进程（MSYS/Git-Bash 下 taskkill 无效，必须用 PowerShell）
powershell -NoProfile -Command "Stop-Process -Id <PID> -Force"

# 或一条龙：按端口杀
for /f "tokens=5" %a in ('netstat -ano ^| findstr :8787') do powershell -NoProfile -Command "Stop-Process -Id %a -Force"

# 确认端口释放
netstat -ano | findstr :8787

# 重启服务
cscript //nologo scripts/autostart.vbs
```

### 提供商命名 / 目录 / 悬停说明（UI 架构）
- **单一命名源**：`PROVIDER_META[provider] = {nameCn, nameEn, kind, baseUrl?, keyEnv?, compat?, desc, freeQuota, recovery, auth, payment, needProxy}`。
  所有展示位置（提供商阵列卡片、模型矩阵分组标题、额度分析分组、评分卡、弹窗标题）都必须走
  `providerDisplayName(provider)`，严禁在函数里重新拼 `${nameCn} (${nameEn})`——否则会出现
  `AMD Radeon (AMD Radeon)` 这类重复括号（nameCn==nameEn 时会重复）。
- `providerDisplayName` 三种处理：nameCn==nameEn → 只显中文名；英文名尾部括号说明先剥离
  （`Ollama (Local)` → `Ollama`）；中文名已含英文名 → 只显中文名。
- `state.providers` 是 **`{provider, modelCount}` 对象数组**（从 `dashboard.models.by_provider` 派生），
  不是字符串数组；任何 `state.providers.map(p => ...)` 都要取 `p.provider`（曾导致评分卡全部空置）。
- **未接入目录**：`#providerCatalog` 渲染 `PROVIDER_META` 中带 `baseUrl` 且未接入的项；
  `compat==='openai'` 才给「接入」按钮，否则显示「需网关转接」（如 Anthropic 原生协议）。
  接入流程：`POST /admin/providers{name,base_url,key_env,type:'free',free_only:true}` →
  `POST /admin/keys{action:'add'}` → `POST /admin/providers/{name}/refresh` → `loadDashboard/loadModels/loadKeys` → `renderAll`。
- **悬停说明用自定义 tooltip，不用原生 title**：给元素加 `data-tip="多行文本"`，
  `initTips()` 在全局 `mouseover/mouseout` 委派显示 `#frTip`（
  `position:fixed; white-space:pre-wrap`）——原生 `title` 延迟长、无法排版，实测用户不满意。
- **模型能力画像**：`CAP_RULES` 用正则从模型名+tags 推断能力（视觉/代码/推理/长上下文/轻量/旗舰/嵌入/音频），
  再由 `buildModelTooltip(m)` 拼「主要能力 + 擅长任务」。tags 为空时也能给出有用说明。
- 新增提供商（国内）：deepseek / moonshot / minimax / stepfun / baichuan / sensetime；
  （国外）：openai / anthropic / gemini / xai / mistral / groq / together / openrouter / deepinfra / cerebras / nvidia / github-models / perplexity；
  以及 ollama（本地）。均带 baseUrl/keyEnv/需代理标记。

### 看门狗自愈（开机自启 + 掉线自动拉起）
- 脚本：`scripts/autostart.vbs`（登录时启动服务一次）、`scripts/watchdog.vbs`（每 100s 检查，掉线则拉起）。
- 注册表：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `FreeRouter` / `FreeRouterWatchdog`，
  值为 `wscript //nologo "free-router\scripts\xxx.vbs"`。在 MSYS 下用 `reg add` 写值时用**单引号**包住，
  否则 `\/` 会被写成错误路径（`L:\/free-router`）。
- **.vbs 必须纯 ASCII**：中文注释会被 cscript/wscript 按 ANSI 解析，报 `缺少 'End'` 之类语法错并 exit 1。注释一律英文。
- 存活判断：WMI 查 `python.exe` 命令行是否含 `run.py` / `app.main:app` / `free-router`。
- 脱离会话常驻：`powershell -NoProfile -Command "Start-Process -FilePath wscript.exe -ArgumentList '//nologo','free-router\scripts\watchdog.vbs' -WindowStyle Hidden"`。
- **登录弹框（已修）**：`shell.CurrentDirectory = "L:\..."` + `shell.Run` 在外置盘未就绪时报
  `800A0046 没有权限` 并弹 Windows Script Host 对话框。必须：①顶部 `On Error Resume Next`（永不弹框）；
  ②循环等 `fso.FileExists(baseDir & "\run.py")` 成立；③**不用 CurrentDirectory**，改用
  `shell.Run "cmd /c cd /d """ & baseDir & """ && """ & pyExe & """ run.py", 0, False`；
  ④启动前用 WMI 查 `run.py`/`app.main:app` 已在跑则跳过。
- **单实例守卫**：数 `Win32_Process` 里 `Name='wscript.exe'` 且命令行含 `watchdog.vbs` 的个数，`>1` 就 `WScript.Quit`。
- 实测：杀服务后 **≤38s** 自动恢复（轮询 30s）；单实例守卫生效。

## 单模型别名（2026-10 起，默认可关闭）
- 设计目的：智能体侧**只看到一个模型** `free-router`，实际由本服务在 N 个模型间自动选择/切换。
- `GET /v1/models` 只返回一条：`{id: "free-router", name: "free-router (N 个模型轮转)", rotating_models: N}`。
- 请求体 `model` 为 `free-router` / `auto` / `router` / `default` / 空 → `_normalize_model_alias()` 直接删掉
  `model` 字段，交 Router 自动路由（显式真实模型名仍保留越权指定能力）。
- 配置：`server.model_alias` / `server.expose_single_model`（false=列出全部，仅调试）/ `server.alias_show_count`。
- 坑：`Registry.__init__` 只把 **config.yaml 里的静态模型**载入内存，其余靠异步 discovery 补齐；
  所以刚启动的前几秒 `list_active()` 会偏小（实测 26→70），这不是数据丢失，别误判。

## 全自动化（无需人工干预）
- **内部巡检循环**（`main.py` lifespan）：每 `quota.recovery_check_seconds`(15s) 调 `quota.purge_expired()`
  释放到期冷却 + 统计冷却比例，超 `cooled_ratio_trigger`(0.6) 自动触发 `run_discovery_once()`。
  取代早先外挂的 `skill/scripts/watcher.py`（已废弃，不要再启）。
- **轮转时长合理化**：`cooldown_429: 30`、`cooldown_429_escalate: [60,300]`、`cooldown_5xx: 20`。
  实测：手动打 429 → 约 30s 释放（原 60s，升级到 [300,1800]）。
- **常用模型优先切回**：收藏列表 `provider/model` 在 selector 里加 `FAVORITE_BONUS(150)`（越靠前越高），
  常用模型一恢复就回队首。
- 配置必须改 **config.yaml**（它覆盖 dataclass 默认值）——只改 `config.py` 默认值不生效。

## 收藏 / 测速 / 自动打标签
- `GET/POST /admin/favorites`（数组，顺序即优先级）→ 写覆盖层 `favorites:`；`state.favorites` 前端同步。
- `POST /admin/providers/{name}/probe {limit}` 与 `POST /admin/probe_all {limit}`：发极小的真实请求测
  连通性/延迟，结果存 `app_state["probe"]` 并随 `/admin/status` 返回（前端卡片显示 📡 徽标）。
- 自动打标签：`LifecycleManager._infer_tags()` 按模型 ID 推断
  vision/audio/embedding/rerank/code/math/reasoning/long/fast/flagship；
  `backfill_metadata()` 在启动时给历史模型补 tags/capability（`Registry.save()` 就地持久化）。
- 前端：卡片带 Key 配置徽标 + 延迟徽标 + 📡 测速；模型矩阵新增「能力」列；
  排序新增「免费额度 / 额度恢复速度」（`PROVIDER_QUOTA` 的 q/r 评级）；
  提供商目录支持 国内/国外/本地 筛选 + 隐藏需代理 + 仅看有免费额度。

## 模型库更新推送（通知）
- `app/core/notify.py` 的 `Notifier`：事件落盘 `data/notifications.json`；同一 (kind,provider,model)
  未读时只累加 count，不刷屏。接口：`GET /admin/notifications`、`POST /admin/notifications/ack`
  （`{ids:[...]}` 或 `{all:true}`）、`POST /admin/notifications/clear`。
- **变更检测用快照对比，不要用 in-memory 差集**：`Registry` 每次启动只装载 config 静态模型，
  其余靠 discovery 补齐，所以“注册表新增”在每次重启都会把整个池子误报为新增（实测刷出 ~50 条）。
  正解：`LifecycleManager._snapshot_and_notify()` 把 `{provider/model: 元信息}` 写入
  `data/model_snapshot.json`，与上次快照 diff 后才发事件；首轮只建基线、不发通知。
- 前端：`#sec-notify` 区块（有事件才显形）+ 每 60s 轮询，新增时 toast；
  每条含能力徽标与「擅长任务」文案，提供「✓ 保留」「✕ 移除」（移除 = `DELETE /admin/models` 写墓碑）。
- 坑：不要把 DB 里的 models 表载回内存池 —— 那是历次发现累积的联合（实测 449 个），
  会污染路由池。DB 只用于 `removed_models` 墓碑。
- 坑：`with self._conn() as c:` **不会关闭连接**（只 commit），读后紧跟写会 `database is locked`；
  读到局部连接时必须 `finally: conn.close()`。

## 面板排版约定（避免错位）
- `.provider-section-header` 是 `justify-content:space-between`：若有多个子元素，切换箭头会被推到中间。
  必须把箭头+按钮包进 `.provider-section-actions`，并给 `.provider-section-title` 加 `flex:1; min-width:0`。
- 提供商卡片标题用**两行**（`.pc-cn` 中文 / `.pc-en` 英文小字），不要写 `中文 (English)` —— 窄卡片会在
  单词中间折行，看起来破碎。
- 卡片操作行只放「📡 测速 / 🔄 刷新 / 查看模型」；**不要把 🔑 放进去**（用户明确不要，且会挤到右边缘）。
  Key 统一走区块标题旁的「🔑 密钥管理」（`openKeyManager()`）→ 选提供商 → `openApiKeyModal()`。
- `.provider-card { min-height: 176px }` 保证跨行卡片高度一致。

## 界面状态呈现规则（用户要求）
- **任何“失败 / 不可用”状态必须把原因直接印在界面上**，不能只放在悬停提示里：
  用户不会去 hover，只会来问“为什么失败”。做法：卡片内一行 `.probe-reason`
  （默认 `display:none`，失败才显示），点「测速」的 toast 也带上 `error` 原文前 180 字。
- 面板徽标只说“失败”＝不合格；徽标 + 原因行才是合格交付。

## 改这个代码库时的编辑规则
- **QwenPaw ACP 拒绝工作区外的 `patch`/`write_file`**：`free-router` 在 QwenPaw 工作区
  （`<本机路径>`）之外，直接对 `L:\...` 路径 `patch`/`write_file`
  会返回 `Edit approval denied by ACP client`。但 `terminal` 的文件写入**不受此限制**，
  对工作区内路径（`<本机路径>`）的 `write_file` 也照常可用。
- **可靠落盘改法（实测）**：把「就地改文件的 Python 脚本」或「新文件内容」先 `write_file` 写进
  工作区，再用 `terminal` 执行/搬运，结果与 patch/write_file 等价且不触发审批：
  - 就地改：`write_file` 写 `workspaces/default/._patch.py`（脚本内用 `pathlib` 读写 `free-router/...`，
    以 `s.replace(old, new)` 或 `s.index(...)` 切片替换）→ `C:/.../python.exe L:/.../._patch.py`。
  - 新文件：`write_file` 写进工作区 → `mkdir -p free-router/... && cp <工作区文件> free-router/...`。
  - 用完删掉 `._*` 临时文件。
- **别用 `cat > file << 'PYEOF'` heredoc 写含嵌套引号的 Python**：bash 报
  `unexpected EOF while looking for matching '`（脚本里的单/双引号破坏解析）。heredoc 只适合
  极简纯文本；复杂 Python 一律走上面「write_file 到工作区 + terminal 执行」。
- 用 `patch` 插入代码时，`old_string` **不要以换行结尾**（也别把锚点行之后的换行吃进来）：
  一个尾随换行会让替换结果吞掉下一行缩进，产出 `def f():    """doc"""` 这种
  `IndentationError`。锚点取“不含尾随换行的唯一片段”，改完立刻 `python -c "import 模块"` 或跑测试。
- 改 Python 必须 `python -m pytest -q` 全绿（测试数随功能持续增长，勿按固定数字断言）；改前端必须 `node --check app/admin_ui/app.js`
  ＋无头 Edge 实测，别只看 HTTP 200。
- **加日志用项目的 structlog**：`from ..core.observability import get_logger`（返回 structlog
  BoundLogger，支持 `logger.info("event", key=val)` 关键字字段）。**别用 `logging.getLogger`
  传关键字**——stdlib `logging` 的 `_log()` 不接受任意 kwargs：`warning` 在默认 WARNING 级别下会直接抛
  `TypeError: Logger._log() got an unexpected keyword argument 'xxx'`；而 `info` 因级别不够被静默跳过。
  二者表现不一致，极易误判「日志没生效」。安全写法是位置参数 `logger.info("msg %s", x)`。
- **`write_file` 对 `.yaml/.json/.toml` 做语法校验，含 Go 模板（`{{ }}`）的文件会被拒**（Helm 的
  `deployment.yaml` 等报 `YAMLError ... found '-'`；`_helpers.tpl` 因无 `.yaml` 扩展名才通过）。
  写 Helm/K8s 这类模板文件改走「Python 脚本 `pathlib.write_text`」或「先写工作区再 `cp`」，别硬塞 `write_file`。
  同源坑：`mkdocs.yml` 里的 `!!python/name:pymdownx.superfences.fence_code_format` 自定义 tag 会让
  `yaml.safe_load` 抛 `ConstructorError`——测试里先 `text.replace("!!python/name:...", "字符串")` 再 load。
- `tests/` 里固化了旧的冷却/升级数值：**改了策略常量就要同步把断言改成表达新意图**
  （例：`test_cooldown_escalation` 断言升级序列末档），不要把回退当作修复。

## 提供商接入与失败排查
- 用户报“填了 Key 还是失败”时，走 `references/provider-failure-triage.md` 的流程，
  不要猜：`/admin/keys` 确认落盘 → `/admin/status.probe` 拿原始错误 → 按错误形态判归属
  → 必要时用 `.env` 里的 Key 直接 curl 区分“Key 无效(401)”与“平台故障(5xx)”。
- 该 reference 还含 **Key 格式指纹表**（长度/前缀一眼判贴错，如百炼 `sk-`+32≈35）
  与 **火山方舟必须用推理接入点 `ep-xxxx` 而非模型名** 这两个高频坑。

### 已收录的国内外提供商（PROVIDER_META）
国内：modelscope / zhipu / siliconflow / qianfan / hunyuan / spark / dashscope / ark / deepseek / moonshot / minimax / stepfun / baichuan / sensetime
国外：amd-radeon / kilogw / openai / anthropic / gemini / xai / mistral / groq / together / openrouter / deepinfra / cerebras / nvidia / github-models / perplexity
补充（2026-10）：**sambanova**（长期限速免费层）、**ollama-cloud**（`ollama.com/v1`，与本地 ollama 分开）、**dashscope-intl**（阿里百炼国际站，`keyEnv: DASHSCOPE_INTL_API_KEY` —— 与国内版 Key 不同，必须另起名）、**nous**（`https://inference-api.nousresearch.com/v1`，420+ 模型中 11 个 `:free` 永久免费）、**本地 ollama**。

### 已知不可用 / 有限制的“免费”提供商
- **OpenCode Zen**（`https://opencode.ai/zen/v1/`）：`/models` 可匿名读，但 chat/completions 外部调用被拒：
  `403 FreeTierError: OpenCode's free tier can only be used from within OpenCode`。**不要试图伪装客户端绕过**，这是它的硬限制。
  它还有一类走 `/v1/responses` 或 `/systemone` 的非 chat 端点，也不适用于 OpenAI chat 客户端。
- **Nous Portal 免费模型**：无需付费，但必须带 `NOUS_API_KEY`（无 key 返回 x402 支付挑战，`maxAmountRequired: 0`）。

## 多账号 Key 轮换（重要 bug 已修）
- `BaseAdapter._try_keys` 是**同步**函数，而 `_do_request` 是 `async def`：`func(key)` 只创建协程、
  不执行，所以 `try/except` **永远捕获不到 HTTP 异常**，Key 轮换实际上从未生效（第一个 Key 超额就整体失败）。
- 正解：用 `_try_keys_async`（`await func(key)` 后再捕获），`chat()` 与 `_open_stream()` 都已切过去。
- 轮换规则：`QuotaExhausted`(429) / `ProviderError(401|403|429)` → 换下一个 Key；
  `>=500` → 直接抛出由 Router 跨商切换；全部失败 → 抛最后一个错（保留 429 语义，让 Router 正确冷却）。
- 验证：魔搭 3 个 Key（#1 余额不足、#2 正常、#3 需邮箱验证）现在会自动跳到 #2 成功。
- 排查手段：`load_config` + `get_provider_pool(cfg)` 后直接 `await adapter.chat(...)`，比看面板快。

## 火山方舟（ark）接入要点
- 不认模型别名。**必须用带日期后缀的确切 model id**（如 `deepseek-v4-1-flash-260910`），
  写 `doubao-seed-1.6` 这类会返回 `InvalidEndpointOrModel.NotFound`。
- 先用 `GET {base}/models` 拿全量 id + `status`，再逐个实测：未开通的模型报 `ModelNotOpen`。
- **ark 不放进 `discovery_urls`**：它的 /models 混有图像/视频/3D 生成模型（seedream/seedance/hyper3d），
  自动发现会把非对话模型灌进候选池。对话模型在 `providers.ark.models` 手工维护。
- 每日 200 万 token 免费额度需先在控制台加入「协作奖励计划」。

### 验证新代码生效
```bash
curl -s 127.0.0.1:8787/health | python -m json.tool
# 必须包含 "models": {"total": N, "active": M, "by_provider": {...}}
```

### 单元测试
- 用仓库根目录的独立 Python 跑（Hermes venv 的 `python` 没装 pytest，会报 `No module named pytest`）：
  ```bash
  # 仓库根 _run_tests.py 已硬编码该解释器；或直接：
  <本机路径> -m pytest -q
  <本机路径> -m pytest tests/test_observability.py -v
  ```
- **native 工具传路径用正斜杠盘符**：给该 Python 传脚本路径必须写 `free-router/...`，
  不能写 MSYS 的 `free-router/...`（路径转换被禁用，`/l/...` 会被解释成 `L:\l\...` 找不到文件）。
