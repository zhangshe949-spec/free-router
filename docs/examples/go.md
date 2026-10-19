# Go 示例

```go
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
)

func main() {
	body, _ := json.Marshal(map[string]any{
		"messages": []map[string]any{{"role": "user", "content": "你好"}},
	})
	resp, err := http.Post("http://127.0.0.1:8787/v1/chat/completions",
		"application/json", bytes.NewReader(body))
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()

	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	fmt.Printf("%v\n", out)
}
```

## 批量

```go
body, _ := json.Marshal(map[string]any{
	"requests": []map[string]any{
		{"messages": []map[string]any{{"role": "user", "content": "a"}}},
		{"messages": []map[string]any{{"role": "user", "content": "b"}}},
	},
	"max_concurrent": 5,
})
resp, _ := http.Post("http://127.0.0.1:8787/v1/batch/completions", "application/json", bytes.NewReader(body))
defer resp.Body.Close()
// 解析 resp.Body（results 数组）
```
