# JavaScript / TypeScript 示例

```js
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://127.0.0.1:8787/v1", apiKey: "not-needed" });

const resp = await client.chat.completions.create({
  model: "free-router",
  messages: [{ role: "user", content: "你好" }],
});
console.log(resp.choices[0].message.content);
```

## 批量（fetch）

```js
const r = await fetch("http://127.0.0.1:8787/v1/batch/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    requests: [
      { messages: [{ role: "user", content: "a" }] },
      { messages: [{ role: "user", content: "b" }] },
    ],
    max_concurrent: 5,
  }),
});
console.log(await r.json());
```

## WebSocket

```js
const ws = new WebSocket("ws://127.0.0.1:8787/v1/ws/chat");
ws.onopen = () => ws.send(JSON.stringify({ messages: [{ role: "user", content: "hi" }], stream: true }));
ws.onmessage = (e) => console.log(e.data);
```
