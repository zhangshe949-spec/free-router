#!/usr/bin/env python3
# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Free-Router 额度守护进程：周期探测额度/冷却，额度耗尽自动轮换模型。
配合 FreeRide-H 的 mid-task 切换思想，下次调用自动反映新模型。"""
import asyncio
import time
import httpx

BASE = "http://127.0.0.1:8787"


async def probe_once():
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{BASE}/admin/status")
            if r.status_code != 200:
                return
            d = r.json()
            cooldowns = d.get("quota", {}).get("cooldowns_active", 0)
            models = d.get("models", {}).get("active", 0)
            print(f"[{time.strftime('%H:%M:%S')}] active={models} "
                  f"cooldown={cooldowns}", flush=True)
            # 若冷却过多（模型不可用），尝试刷新
            if models > 0 and cooldowns >= models:
                print("  大量冷却，触发自动刷新...")
                await client.post(f"{BASE}/admin/refresh")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] probe error: {e}", flush=True)


async def main():
    interval = float(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 60.0
    print(f"Free-Router watcher 启动，每 {interval}s 探测一次")
    while True:
        await probe_once()
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())