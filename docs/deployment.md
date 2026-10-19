# 部署

## Docker

```bash
docker build -t free-router .
docker run -d -p 8787:8787 \
  -e RADEON_API_KEY=rc-xxx \
  -v $PWD/config:/app/config \
  free-router
```

## Docker Compose

```bash
docker compose up -d
```

## Helm

```bash
helm install free-router ./charts/free-router \
  --set config.server.access_key=secret \
  --set-json config.providers='{"kilogw":{"base_url":"https://api.kilo.ai/api/openrouter"}}'
```

Chart 结构：

```
charts/free-router/
  Chart.yaml
  values.yaml                 # replicas/resources/autoscaling/ingress/config/secret/serviceMonitor/...
  templates/
    deployment.yaml
    service.yaml
    configmap.yaml            # config.yaml（values.config 全量覆盖）
    secret.yaml               # API keys（envFrom）
    servicemonitor.yaml
    pdb.yaml
    hpa.yaml
    ingress.yaml
    _helpers.tpl
```

`values.config` 可覆盖 `config.yaml` 任意字段；`values.secret.data`（或 `existingSecret`）注入 API 密钥为环境变量。

## Kubernetes Operator（TODO）

- CRD `FreeRouter`（spec：replicas / config / image / resources / autoscaling）
- Controller Reconcile：滚动更新、配置热重载触发、状态上报

> 暂未实现，规划见 `operator/` 目录（如时间允许后续补齐）。

## GitHub Pages（文档）

```bash
pip install mkdocs-material mkdocstrings[python] pymdown-extensions
mkdocs build            # 生成 site/
mkdocs gh-deploy        # 部署到 GitHub Pages
```
