# Python 示例

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="not-needed")

# 普通对话
resp = client.chat.completions.create(
    model="free-router",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="free-router",
    messages=[{"role": "user", "content": "写一首诗"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")

# 任务上下文（优先用免费推理模型做代码生成）
resp = client.chat.completions.create(
    model="free-router",
    messages=[{"role": "user", "content": "写个快排"}],
    extra_body={"free_router": {"task_type": "code_generation", "prefer_free": True}},
)
```

## 批量 + WebSocket

```python
import json, asyncio, httpx, websockets

async def batch():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8787") as c:
        r = await c.post("/v1/batch/completions", json={
            "requests": [
                {"messages": [{"role": "user", "content": "a"}]},
                {"messages": [{"role": "user", "content": "b"}]},
            ],
            "max_concurrent": 5,
        })
        print(r.json())

async def ws():
    async with websockets.connect("ws://127.0.0.1:8787/v1/ws/chat") as ws:
        await ws.send(json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": True}))
        async for m in ws:
            print(m)

asyncio.run(batch())
asyncio.run(ws())
```
