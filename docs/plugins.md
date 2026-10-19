# 插件

插件框架（P9）：启动时动态加载，挂载到请求生命周期。

## 配置

```yaml
plugins:
  - name: request-logger
    module: app.plugins.builtin
    class: RequestLoggerPlugin
    config: {}
  - name: response-transformer
    module: app.plugins.builtin
    class: ResponseTransformerPlugin
    config:
      strip_keys: [reasoning_content, thinking, reasoning]
```

## 钩子

插件继承 `app.plugins.base.BasePlugin`，覆写需要的钩子（均 async）：

| 钩子 | 时机 | 参数 |
|---|---|---|
| `on_request(request, context)` | 请求进入（中间件） | 请求对象 + 共享 context |
| `on_response(response, context)` | 响应生成后（端点层，dict 响应） | 响应 dict + context |
| `on_error(error, context)` | 处理异常（中间件） | 异常 + context |

`context` 为一次请求共享的可变 dict（`on_request` 写、`on_response` 读）。

## 内置插件

- **RequestLoggerPlugin** —— 结构化日志请求/响应（method / path / status / 耗时）。
- **ResponseTransformerPlugin** —— 响应变换，剔除 `reasoning_content` / `thinking` 等思考链字段。

## 自定义插件

```python
# my_plugins.py
from app.plugins.base import BasePlugin

class AddHeaderPlugin(BasePlugin):
    name = "add-header"
    async def on_request(self, request, context):
        context["custom"] = True
    async def on_response(self, response, context):
        if isinstance(response, dict):
            response["_custom"] = "yes"
```

```yaml
plugins:
  - name: add-header
    module: my_plugins
    class: AddHeaderPlugin
    config: {}
```

加载失败只告警、不中断启动。
