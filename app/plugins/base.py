# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""插件基类：请求生命周期钩子（雏形）。

钩子全部 async 且默认空实现，插件可只覆写自己关心的钩子。
context 为一次请求共享的可变 dict（在 on_request 里写、on_response 里读）。
"""
from __future__ import annotations

from typing import Any, Optional


class BasePlugin:
    """插件抽象基类。

    钩子：
      on_request(request, context)   —— 请求进入时（body 尚未消费）
      on_response(response, context) —— 响应生成后（response 为 dict 或 Response）
      on_error(error, context)       —— 处理异常时
    """

    name: str = "base"

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    async def on_request(self, request: Any, context: dict) -> None:
        """请求进入时调用。"""

    async def on_response(self, response: Any, context: dict) -> None:
        """响应生成后调用。"""

    async def on_error(self, error: BaseException, context: dict) -> None:
        """处理异常时调用。"""
