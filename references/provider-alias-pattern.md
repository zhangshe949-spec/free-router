# 提供商别名 / 多账号 Key 隔离模式

## 背景
free-router 所有客户端共享同一 `~/.free-router/.env`，同一提供商默认一个 Key。当需要同一提供商的不同账号 Key 时，有三种隔离方案。

---

## 方案对比

| 方案 | 隔离级别 | 配置复杂度 | 适用场景 |
|------|----------|------------|----------|
| **提供商别名** | 模型级隔离 | 低（改配置文件） | 同一提供商不同 Key，想在同一实例里共存 |
| **多实例部署** | 进程级完全隔离 | 中（多端口、多进程） | 完全隔离，不同团队/项目/环境 |
| **租户头隔离** | 逻辑级隔离 | 无（仅加请求头） | 同一 Key，但要区分调用方额度/日志 |

---

## 方案 1：提供商别名（推荐用于多账号同提供商）

### 核心思路
在 `config.yaml` 的 `providers` 列表中把同一提供商写两遍，起不同名，各自指向不同的 `key_env` 变量。

### 配置示例

**config.yaml**
```yaml
providers:
  - name: zhipu-main
    type: zhipu
    base_url: https://open.bigmodel.cn/api/paas/v4
    key_env: ZHIPU_API_KEY_MAIN
    models:
      - glm-4-flash
      - glm-4.7-flash
  - name: zhipu-backup
    type: zhipu
    base_url: https://open.bigmodel.cn/api/paas/v4
    key_env: ZHIPU_API_KEY_BACKUP
    models:
      - glm-4-flash
      - glm-4.7-flash
```

**.env**
```bash
ZHIPU_API_KEY_MAIN=xxxxxxxx
ZHIPU_API_KEY_BACKUP=yyyyyyyy
```

### 效果
- `/v1/models` 返回两组模型，`provider` 字段分别为 `zhipu-main` / `zhipu-backup`
- 智能体侧看到两个独立的提供商，可分别选择
- 额度、冷却、缓存按 provider 名分离
- 无需重启，`/admin/refresh` 热生效

---

## 方案 2：多实例部署

### 核心思路
启动多个 free-router 进程，分别监听不同端口，各自读取独立的配置/环境变量。

### 启动脚本示例
```bash
# 实例 A
FREE_ROUTER_CONFIG=/path/to/config-a.yaml ~/.free-router-a/.env python run.py --port 8787

# 实例 B
FREE_ROUTER_CONFIG=/path/to/config-b.yaml ~/.free-router-b/.env python run.py --port 8788
```

### 客户端配置
- QwenPaw / Hermes 分别填入 `http://127.0.0.1:8787/v1` 和 `http://127.0.0.1:8788/v1`
- 完全独立，互不干扰

---

## 方案 3：租户头隔离（已内置支持）

### 核心思路
请求头加 `X-Free-Router-Tenant: team-a` / `team-b`，额度、冷却、缓存自动按 tenant 隔离，**Key 仍共用一个**。

### 使用示例
```bash
curl -H "X-Free-Router-Tenant: team-a" http://127.0.0.1:8787/v1/chat/completions ...
```

### 适用场景
- 同一 Key，但要区分不同团队/项目的额度消耗
- 审计日志按 tenant 区分
- 不想维护多份 Key 配置

---

## 决策建议

- **个人/小团队、同一提供商多账号** → **提供商别名**（最简单、热刷新、单进程）
- **多团队/多环境、严格隔离** → **多实例部署**
- **同一 Key、仅需额度/日志隔离** → **租户头**

---

## 实现细节提示

1. 提供商别名时，`type` 字段保持原提供商类型（如 `zhipu`），`name` 改为别名
2. 别名提供商会共享同一套 provider 类代码（`app/providers/zhipu.py`），只差 Key
3. 模型去重：聚合列表中若同一模型出现在多个别名下，路由器按优先级/健康度选其一
4. 管理面板「提供商阵列」会分别显示每个别名及其模型数量