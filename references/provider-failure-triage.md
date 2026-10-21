# 提供商「测速失败 / 调用报错」排查

用户说“我已经填了 Key，怎么还是失败？”时，**按此顺序定位，不要猜**。

## 1. 先确认 Key 到底有没有落盘
```bash
curl -s 127.0.0.1:8787/admin/keys
```
`count > 0` 才算配置成功；`count = 0` 说明没填进去或没热重载。

## 2. 拿到提供商返回的**原始错误**
面板徽标只显示“失败”，原因在 `/admin/status` 里：
```bash
curl -s 127.0.0.1:8787/admin/status > /tmp/s.json
# .probe.<provider>.models[].error 就是上游原文
```
或直接点面板卡片的「📡 测速」（toast 会带原因前 180 字）。

## 3. 按错误形态判归属
| HTTP | 典型 message | 归属 | 处理 |
|---|---|---|---|
| 401 | `Incorrect API key provided` | Key 错 / 失效 / 贴错 | 重新复制 |
| 429 / rate limited | `insufficient balance`、`Daily usage limit exceeded` | 额度，不是 bug | 换模型 / 等恢复 |
| 404 | `InvalidEndpointOrModel.NotFound` | 模型 ID 不对 | 见第 5 节（火山方舟） |
| 5xx | `RTS service is unavailable`(60001/60017) | 平台侧 | 重试 / 查账号限制 |
| — | 模型已下线、`upstream ... overloaded` | 上游变动 | 无需处理，会自动切换 |

## 4. 区分“Key 无效”还是“平台故障”
脱离 free-router，直接用 `.env` 里的 Key 打一次（本机需 `--ssl-no-revoke`）：
```bash
set -a; . "$HOME/.free-router/.env"; set +a
curl -s -o /dev/null -w '%{http_code}\n' --ssl-no-revoke \
  https://api.<provider>.com/v1/models -H "Authorization: Bearer $<ENV_NAME>"
```
**401 = Key 问题；200/5xx = Key 已通过鉴权，是平台或额度问题。** 这一条能把“是不是我填错了”与“平台挂了”彻底分开。

## 5. Key 格式指纹（一眼看出贴错）
| 提供商 | 形态 | 典型长度 |
|---|---|---|
| 智谱 ZAI_API_KEY | 32 位 hex + `.` + 16 位 | 49 |
| 阿里百炼 DASHSCOPE_API_KEY | `sk-` + 32 位 | ~35 |
| 火山方舟 ARK_API_KEY | `ark-` 开头 | ~46 |
| 硅基流动 SILICONFLOW_API_KEY | `sk-` + 48 位 | 51 |
| 魔搭 MODELSCOPE_API_KEY | `ms-` 开头 | ~39 |
| AMD RADEON_API_KEY | `rc-` 开头 | — |

> 实测：阿里百炼被填成 **117 字符、`sk-ws-` 开头**的值 → 必定不是百炼 Key（百炼是 `sk-`+32）。
| 长度/前缀对不上就直接判定“贴错”，比反复重试快。

## 6. 火山方舟特例（最容易踩）
方舟的 OpenAI 兼容端点**不接受模型名，只接受推理接入点 ID**（`ep-2026xxxxxx-xxxxx`）：
1. 控制台「在线推理」→ 创建**推理接入点**，绑定 doubao / deepseek-v3.1 等
2. 把生成的 `ep-xxxxx` 当作模型 id 填进池子
报 `InvalidEndpointOrModel.NotFound: doubao-seed-1.6 does not exist or you do not have access` 就是这个原因。

## 7. 怎么回复用户
给一张表：**提供商 / 原始错误 / 归属 / 你要做什么**，并明确一句
“**Key 填写机制没问题** —— 只要有一家测速成功（如智谱 496ms）就证明
面板→`.env`→热重载整条链路是通的”，避免用户以为是自己操作错了。
