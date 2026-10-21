#!/usr/bin/env python3
# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""free-router 性能基准脚本。
用法: python bench/bench.py [--host 127.0.0.1:8787] [--out bench_report.md]
依赖: httpx (pip install httpx)
"""
import argparse
import asyncio
import csv
import statistics
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("缺少 httpx: pip install httpx")

BASE = "http://127.0.0.1:8787"
CHAT_MSG = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hello in one sentence."}], "max_tokens": 30}
BATCH_REQ = {"requests": [{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 10}] * 5, "max_concurrent": 4}


async def _one_chat(c, sem):
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await c.post("/v1/chat/completions", json=CHAT_MSG, timeout=30)
            ok = r.status_code == 200
        except Exception:
            ok = False
        return time.perf_counter() - t0, ok


async def bench_chat(concurrency, duration):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        t0 = time.perf_counter()
        lat, errs = [], 0
        while time.perf_counter() - t0 < duration:
            tasks = [_one_chat(c, sem) for _ in range(concurrency)]
            res = await asyncio.gather(*tasks)
            for dt, ok in res:
                lat.append(dt)
                if not ok:
                    errs += 1
    return lat, errs


async def bench_batch(concurrency, duration):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        t0 = time.perf_counter()
        lat, errs = [], 0
        while time.perf_counter() - t0 < duration:
            tasks = []
            for _ in range(concurrency):
                async def _b():
                    async with sem:
                        t = time.perf_counter()
                        try:
                            r = await c.post("/v1/batch/completions", json=BATCH_REQ, timeout=60)
                            ok = r.status_code == 200
                        except Exception:
                            ok = False
                        return time.perf_counter() - t, ok
                tasks.append(_b())
            res = await asyncio.gather(*tasks)
            for dt, ok in res:
                lat.append(dt)
                if not ok:
                    errs += 1
    return lat, errs


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def report(name, lat, errs, rows):
    n = len(lat)
    if n == 0:
        return
    qps = n / (sum(lat) / n) if n else 0
    rows.append({
        "name": name, "requests": n, "errors": errs,
        "qps": round(qps, 2),
        "p50": round(pct(lat, 50) * 1000, 2),
        "p95": round(pct(lat, 95) * 1000, 2),
        "p99": round(pct(lat, 99) * 1000, 2),
        "avg_ms": round(statistics.mean(lat) * 1000, 2),
        "err_rate": round(errs / n * 100, 4),
    })


async def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=BASE)
    ap.add_argument("--out", default="bench_report.md")
    a = ap.parse_args()
    BASE = a.host
    rows = []
    print("== chat (c=50, 30s) ==")
    lat, errs = await bench_chat(50, 30)
    report("chat", lat, errs, rows)
    print("== batch (c=20, 30s) ==")
    lat, errs = await bench_batch(20, 30)
    report("batch", lat, errs, rows)
    out = Path(a.out)
    md = ["# free-router 性能基准报告", "", "| 场景 | 请求数 | 错误 | QPS | P50(ms) | P95(ms) | P99(ms) | 错误率 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['name']} | {r['requests']} | {r['errors']} | {r['qps']} | {r['p50']} | {r['p95']} | {r['p99']} | {r['err_rate']}% |")
    md += ["", f"主机: `{BASE}`", f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    out.write_text("\n".join(md), encoding="utf-8")
    csvp = out.with_suffix(".csv")
    with open(csvp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["name"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n报告: {out}\n原始: {csvp}")


if __name__ == "__main__":
    asyncio.run(main())