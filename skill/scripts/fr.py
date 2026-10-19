#!/usr/bin/env python3
"""Free-Router CLI：Skill 层与代理层的交互协议。"""
import argparse
import json
import sys
import httpx

BASE = "http://127.0.0.1:8787"


def _req(method, path, json_body=None):
    try:
        if method == "GET":
            r = httpx.get(f"{BASE}{path}", timeout=30)
        else:
            r = httpx.post(f"{BASE}{path}", json=json_body or {}, timeout=60)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        print(f"❌ 调用失败: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status():
    d = _req("GET", "/admin/status")
    print("=== 模型注册表 ===")
    print(json.dumps(d.get("models", {}), ensure_ascii=False, indent=2))
    print("\n=== 额度快照 ===")
    q = d.get("quota", {})
    print(f"冷却中: {q.get('cooldowns_active', 0)}")
    for k, v in q.get("usage", {}).items():
        print(f"  {k}: RPM={v['rpm']} RPD={v['rpd']} TPM={v['tpm']} TPD={v['tpd']}")


def cmd_refresh():
    d = _req("POST", "/admin/refresh")
    print(f"✅ 刷新完成: {json.dumps(d.get('models', {}), ensure_ascii=False)}")


def cmd_switch(args):
    # 支持 provider/model 或 model 两种
    if "/" in args.model:
        p, m = args.model.split("/", 1)
    else:
        p, m = None, args.model
    if p:
        _req("POST", "/admin/switch", {"model": m, "provider": p})
        print(f"✅ 已强制切换到 {p}/{m}")
    else:
        print("请指定 provider/model，如: fr switch amd-radeon/DeepSeek-V4-Flash")


def cmd_cooldown(args):
    _req("POST", "/admin/cooldown",
         {"provider": args.provider, "model": args.model, "reason": args.reason})
    print(f"✅ 已冷却 {args.provider}/{args.model} ({args.reason})")


def main():
    ap = argparse.ArgumentParser(prog="fr", description="Free-Router CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看状态")
    sub.add_parser("refresh", help="刷新模型")
    sp = sub.add_parser("switch", help="切换模型")
    sp.add_argument("model", help="provider/model 或 model")
    cp = sub.add_parser("cooldown", help="手动冷却")
    cp.add_argument("provider")
    cp.add_argument("model")
    cp.add_argument("reason", choices=["429", "5xx", "401", "model_not_found"])

    args = ap.parse_args()
    {"status": cmd_status, "refresh": cmd_refresh,
     "switch": cmd_switch, "cooldown": cmd_cooldown}[args.cmd](args)


if __name__ == "__main__":
    main()