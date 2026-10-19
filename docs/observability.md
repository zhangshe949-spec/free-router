# 可观测性

基于 OpenTelemetry + structlog + Prometheus 指标 + 熔断器 + 健康检查 + 请求队列。

## 日志（structlog）

`observability.log_level`、`observability.json_logs`。结构化 JSON 日志带 `request_id` 关联。

## 追踪（OTLP 导出）

```yaml
observability:
  otlp_endpoint: "http://jaeger:4317"   # OTLP/HTTP
  otlp_headers: {}
  sampling_ratio: 1.0
  propagators: [tracecontext, baggage]
  console_export: false
```

自定义 Span：

| Span 名 | 说明 |
|---|---|
| `free-router.router.select` | 模型选择 |
| `free-router.provider.call` | 上游调用 |
| `free-router.cache.hit` / `cache.miss` | 缓存命中/未命中 |
| `free-router.circuit-breaker` | 熔断器状态变更 |
| `free-router.queue.wait` | 请求排队等待 |

## 指标导出

- Prometheus：`GET /metrics`（零依赖，标准库实现）。
- OTLP：配置 `otlp_endpoint` 后，`PeriodicExportingMetricReader` 按 `metric_export_interval`（默认 60s）导出。

## 健康检查 / 熔断器 / 队列

| 组件 | 端点 | 说明 |
|---|---|---|
| 健康检查 | `/admin/health-check` | 定期 ping 各提供商，连续失败标记不健康 |
| 熔断器 | `/admin/circuit-breakers` | 失败率超阈值打开，窗口期后 half-open 探测 |
| 请求队列 | `/admin/queue` | 全提供商熔断时的背压队列 |
