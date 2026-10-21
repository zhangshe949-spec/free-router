# -*- coding: utf-8 -*-
# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""快速启动 Free-Router 服务（Windows 便捷脚本）"""
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent


def main():
    # 1. 依赖检查：仅在缺失时安装（避免每次启动都跑 pip）
    try:
        import fastapi, uvicorn, httpx, yaml  # noqa: F401
    except ImportError:
        print("检查依赖...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        str(ROOT / "requirements.txt")], check=True)
    # 2. 启动服务
    print("启动 Free-Router @ http://127.0.0.1:8787 ...")
    subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app",
                    "--app-dir", str(ROOT),
                    "--host", "127.0.0.1", "--port", "8787"])


if __name__ == "__main__":
    main()