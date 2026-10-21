# Free-Router 服务镜像（OpenAI 兼容的免费/收费模型聚合路由网关）
# 与任何 agent 框架解耦：容器只暴露一个 OpenAI 兼容端点。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1     FREE_ROUTER_CONFIG=/app/config/config.yaml

WORKDIR /app

# 先装依赖，充分利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 配置与数据（SQLite 用量、日志）建议挂载出来，便于持久化与热更新模型池
VOLUME ["/app/config", "/app/data"]

EXPOSE 8787

# 密钥通过 -e / --env-file 注入，不要打进镜像
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
