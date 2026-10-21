# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""free-router 命令行入口：跨平台启动服务，任何 agent 都能直接调用。

用法:
    free-router                      # 默认 127.0.0.1:8787
    free-router --host 0.0.0.0 --port 8080
    FREE_ROUTER_CONFIG=/path/config.yaml free-router
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="free-router",
                                     description="免费/收费模型聚合路由网关（OpenAI 兼容）")
    parser.add_argument("--host", default=os.environ.get("FREE_ROUTER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("FREE_ROUTER_PORT", "8787")))
    parser.add_argument("--config", default=os.environ.get("FREE_ROUTER_CONFIG"))
    parser.add_argument("--log-level", default=os.environ.get("FREE_ROUTER_LOG_LEVEL", "info"))
    parser.add_argument("--reload", action="store_true", help="开发模式：改动自动重载")
    args = parser.parse_args(argv)

    if args.config:
        os.environ["FREE_ROUTER_CONFIG"] = os.path.abspath(args.config)

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖 uvicorn，请先执行: pip install free-router", file=sys.stderr)
        return 2

    uvicorn.run("app.main:app", host=args.host, port=args.port,
                log_level=args.log_level, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
