# 提供商

内置提供商适配器位于 `app/providers/`。未知提供商自动走 OpenAI 兼容适配器。

| 提供商 | 类型 | 免 key | 说明 |
|---|---|---|---|
| amd-radeon | free | 否 | AMD Radeon API（rc-xxx key） |
| modelscope | free | 否 | ModelScope 魔搭推理 |
| kilogw | free | 是 | Kilo 聚合网关（匿名，仅 `:free` 档位） |
| zen | free | 是 | opencode Zen 匿名免费车道（强制流式） |
| zhipu | free | 否 | 智谱 GLM（中文强） |
| siliconflow | free | 否 | 硅基流动（9B 以下免费） |
| qianfan | free | 否 | 百度千帆 |
| hunyuan | free | 否 | 腾讯混元 |
| spark | free | 否 | 讯飞星火 |
| dashscope | free | 否 | 阿里云百炼 |
| ark | free | 否 | 火山方舟（模型需带日期后缀 id） |

## Key 申请

- AMD：https://developer.amd.com.cn/radeon
- ModelScope：https://modelscope.cn（API-KEY）
- 智谱：https://open.bigmodel.cn
- 硅基流动：https://siliconflow.cn
- 火山方舟：https://console.volcengine.com/ark

## 已知限制

- **ark** 不放进自动发现：其 `/models` 混有图像/视频/3D 生成模型，对话模型需手工维护。
- **zen** 直接调用会被指纹闸门拒（"free tier can only be used from within OpenCode"），适配器内部补齐会话头 + 指纹 + 强制流式。
- **kilogw** 聚合网关只接纳免费档位（`:free` 后缀 / `kilo-auto/free`），付费模型需显式指定。

## 新增提供商

动态新增（无需重启，写入覆盖层）：

```bash
curl -X POST http://127.0.0.1:8787/admin/providers \
  -d '{"name":"my-gw","base_url":"https://example.com/v1","type":"free","key_env":"MY_GW_KEY"}'
```

或在 `config.yaml` 的 `providers` 下新增一段（OpenAI 兼容即可）。
