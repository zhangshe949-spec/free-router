# -*- coding: utf-8 -*-
"""免费模型池实测探针：对每个候选模型发一次最小请求，输出真实可用性。

用法：python scripts/probe_pool.py [--full]
输出：data/probe_report.json + 控制台表格
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.core.config import load_config  # noqa: E402

CFG = load_config(BASE / "config" / "config.yaml")

# 待测清单：provider -> [model...]
def build_targets(full: bool = False):
    t = {}
    # AMD：静态 4 个 + （full 时）全部 10 个
    amd = ["DeepSeek-V4-Flash-Vision-Exp", "DeepSeek-V4-Flash",
           "Qwen3.8-Flash-Next", "DeepSeek-V4.1-Flash"]
    t["amd-radeon"] = amd
    # ModelScope：抽 6 个代表性模型（账号级 403 会全灭，不必全打 35 个）
    t["modelscope"] = ["Qwen/Qwen3.8-Flash-Next", "deepseek-ai/DeepSeek-V4.1-Flash",
                       "Qwen/Qwen3.5-27B", "ZhipuAI/GLM-4.7-Flash",
                       "MiniMax/MiniMax-M3", "stepfun-ai/Step-3.7-Flash"]
    # Kilo 免费池：全测
    t["kilogw"] = ["kilo-auto/free",
                   "nvidia/nemotron-3-ultra-550b-a55b:free",
                   "nvidia/nemotron-3-super-120b-a12b:free",
                   "qwen/qwen3.8-27b:free",
                   "poolside/laguna-s-2.1:free",
                   "nex-agi/nex-n2.5-mini:free",
                   "liquid/lfm-2.5-2.6b:free",
                   "thinkingmachines/inkling-small:free",
                   "dots-studio/dots-3-note-preview:free",
                   "cohere/north-mini-code:free",
                   "nvidia/nemotron-3.5-lightning:free",
                   "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"]
    return t


def classify(status, text: str) -> str:
    tl = (text or "").lower()
    if status == 200:
        return "OK"
    if status == 429 or "rate_limit" in tl or "usage limit" in tl or "limit exceeded" in tl:
        return "429限流/超额"
    if status in (401, 403):
        if "verif" in tl or "profile" in tl:
            return "403待邮箱验证"
        return f"{status}认证失败"
    if status == 404:
        return "404模型不存在"
    return f"{status}"


async def probe_one(client, provider, model):
    pc = CFG.providers[provider]
    url = pc.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if pc.resolved_key:
        headers["Authorization"] = f"Bearer {pc.resolved_key}"
    for k, v in (pc.extra_headers or {}).items():
        headers[k] = v
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5}
    t0 = time.time()
    try:
        r = await client.post(url, json=body, headers=headers)
        dt = round(time.time() - t0, 1)
        txt = r.text[:200]
        return {"provider": provider, "model": model, "http": r.status_code,
                "verdict": classify(r.status_code, txt), "sec": dt,
                "snippet": txt.replace("\n", " ")[:120]}
    except Exception as e:
        return {"provider": provider, "model": model, "http": None,
                "verdict": f"异常:{type(e).__name__}", "sec": round(time.time() - t0, 1),
                "snippet": str(e)[:120]}


async def main():
    full = "--full" in sys.argv
    targets = build_targets(full)
    sem = asyncio.Semaphore(6)

    async def worker(client, p, m):
        async with sem:
            return await probe_one(client, p, m)

    async with httpx.AsyncClient(timeout=30) as client:
        jobs = [worker(client, p, m) for p, ms in targets.items() for m in ms]
        results = await asyncio.gather(*jobs)

    ok = [r for r in results if r["verdict"] == "OK"]
    bad = [r for r in results if r["verdict"] != "OK"]
    print(f"\n=== 实测 {len(results)} 个模型：可用 {len(ok)} / 不可用 {len(bad)} ===\n")
    print(f"{'provider':12s} {'verdict':16s} {'http':5s} {'sec':5s} model")
    for r in sorted(results, key=lambda x: (x["verdict"] != "OK", x["provider"])):
        print(f"{r['provider']:12s} {r['verdict']:16s} {str(r['http']):5s} "
              f"{str(r['sec']):5s} {r['model']}")
    out = BASE / "data" / "probe_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入: {out}")
    # 按 provider 汇总
    print("\n=== 按提供商汇总 ===")
    for p in targets:
        rs = [r for r in results if r["provider"] == p]
        good = [r["model"] for r in rs if r["verdict"] == "OK"]
        reasons = sorted({r["verdict"] for r in rs if r["verdict"] != "OK"})
        print(f"{p}: 可用 {len(good)}/{len(rs)}" +
              (f"  失败原因: {', '.join(reasons)}" if reasons else ""))


if __name__ == "__main__":
    asyncio.run(main())
