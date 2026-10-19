# Web 管理面板 / Docker / CI

本文档说明如何启用内置的 Web 管理面板 `static/index.html`，以及用 Docker 启动服务。

---

## 1. 启用 Web 管理面板

面板是单文件静态页（`static/index.html`，内联 CSS/JS，无外部依赖），通过同源 `fetch`
调用后端的 `/admin/*` 与 `/v1/models` 接口。

FastAPI 默认不会自动托管静态文件，**需要由后端（`app/main.py`）挂载 `StaticFiles`**。
由于本项目采用「后端 / 静态资源」并行开发，面板侧只提供静态文件，挂载由主任务在后端完成。

### 需要添加的两行代码

在 `app/main.py` 中：

1）文件顶部导入区加一行（建议紧挨 `from fastapi.responses import ...` 之后）：

```python
from fastapi.staticfiles import StaticFiles
```

2）在 `app = FastAPI(title="Free-Router", lifespan=lifespan)` **之后**、所有路由定义
**之前或之后均可**，加一行挂载。推荐挂在独立前缀 `/panel`，避免与 `/v1`、`/admin`、
`/health` 等 API 路径冲突：

```python
app.mount("/panel", StaticFiles(directory="static", html=True), name="panel")
```

> 位置建议：放在文件末尾（所有 `@app.get/post` 路由定义之后）最稳妥，
> 确保 API 路由先于静态挂载注册，任何路径都不会被 StaticFiles 抢占。
> `html=True` 使 `http://<host>:8787/panel/` 直接返回 `index.html`。

### 访问

- 本地：`http://127.0.0.1:8787/panel/`
- 面板包含：模型统计（总数/活跃/按提供商分布）、额度用量表（RPM/RPD/TPM/TPD）、
  活跃冷却列表；可搜索过滤的模型列表（id + 提供商 + 是否 vision）；
  以及「重新发现模型 / 重置冷却 / 强制冷却 / 强制切换」按钮。
- 状态区每 **10 秒自动刷新**一次；顶部指示服务在线/离线。

---

## 2. Docker 启动

依赖构建上下文为项目根目录 `L:/free-router`。

```bash
# 构建 + 启动（后台）
docker compose up -d --build

# 查看日志
docker compose logs -f free-router

# 健康检查
curl http://127.0.0.1:8787/health

# 停止
docker compose down
```

- 端口：容器内 `8787` 映射到宿主 `8787`（`docker-compose.yml`）。
- 卷：`./data` 与 `./config` 挂载进容器，SQLite 与配置持久化到宿主机。
- 密钥：存在 `.env` 时通过 `env_file` 注入（`required: false`，缺失不报错）。
  参考 `config/.env.example` 复制为根目录 `.env` 并填写。

### 说明：容器内的配置路径

后端当前把配置路径硬编码为 `L:/free-router/config/config.yaml`。在 Linux 容器中这是相对
路径，会解析到 `/app/L:/free-router/config/config.yaml`。`Dockerfile` 中已用一条符号链接
`/app/L:/free-router -> /app` 将其桥接到底层挂载，因此容器**无需改动后端**即可运行。
待后端改为相对路径 / 环境变量后，该链接无副作用、可删除。

---

## 3. CI（GitHub Actions）

`.github/workflows/ci.yml` 在 `push` 与 `pull_request` 时执行：

1. 安装 Python 3.11；
2. `pip install -r requirements.txt`；
3. 冒烟：`python -c "import app.main"`；
4. 测试：`pytest -q`。

---

## 4. 验证命令

```bash
# HTML 可解析
python -c "import html.parser,sys; html.parser.HTMLParser().feed(open('static/index.html',encoding='utf-8').read())"

# YAML 合法
python -c "import yaml;yaml.safe_load(open('docker-compose.yml',encoding='utf-8'))"
python -c "import yaml;yaml.safe_load(open('.github/workflows/ci.yml',encoding='utf-8'))"
```