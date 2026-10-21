# -*- coding: utf-8 -*-
# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Free-Router 无窗口启动入口（供 pythonw + 计划任务调用）。
自包含：内部 chdir + sys.path，不依赖调用方工作目录。"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))          # .../scripts
BASE = os.path.dirname(ROOT)                                # skill 根
os.chdir(BASE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# pythonw.exe 无控制台：sys.stdout/stderr 为 None，uvicorn logging 一写就崩。
# 必须在 import uvicorn 之前重定向到日志文件（logging 配置会捕获 sys.stderr 引用）。
if sys.stderr is None or sys.stdout is None:
    log_path = os.path.join(BASE, "data", "free_router.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _log = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8787, log_level="info")
