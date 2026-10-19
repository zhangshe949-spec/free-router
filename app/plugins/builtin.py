"""内置示例插件。

RequestLoggerPlugin：结构化日志记录请求/响应（method/path/status/耗时）。
ResponseTransformerPlugin：响应变换——从 dict 响应中剔除「思考链」字段
（reasoning_content / thinking / reasoning），可配置 strip_keys。
"""
from __future__ import annotations

import time

from ..core.observability import get_logger
from .base import BasePlugin

logger = get_logger("free_router.plugin")


def _path_str(v) -> str:
    if v is None:
        return ""
    return str(v)


class RequestLoggerPlugin(BasePlugin):
    """结构化日志请求/响应。"""

    name = "request-logger"

    async def on_request(self, request, context) -> None:
        context["_started"] = time.time()
        context.setdefault("_method", getattr(request, "method", None))
        context.setdefault("_path", _path_str(getattr(request, "url", None)))

    async def on_response(self, response, context) -> None:
        started = context.get("_started") or time.time()
        dur_ms = round((time.time() - started) * 1000.0, 1)
        status = context.get("_status")
        if status is None and not isinstance(response, dict):
            status = getattr(response, "status_code", None)
        logger.info(
            "request_logged",
            method=context.get("_method"),
            path=context.get("_path"),
            status=status,
            duration_ms=dur_ms,
        )

    async def on_error(self, error, context) -> None:
        logger.warning(
            "request_error",
            method=context.get("_method"),
            path=context.get("_path"),
            error=str(error)[:200],
        )


class ResponseTransformerPlugin(BasePlugin):
    """响应变换：剔除 dict 响应里的思考链字段。"""

    name = "response-transformer"

    def __init__(self, config=None):
        super().__init__(config)
        self.strip_keys = list(
            config.get("strip_keys") or ["reasoning_content", "thinking", "reasoning"]
        )

    async def on_response(self, response, context) -> None:
        if not isinstance(response, dict):
            return
        for k in self.strip_keys:
            response.pop(k, None)
        # 也剔除嵌套在 message 里的思考链字段
        for choice in (response.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message")
            if isinstance(msg, dict):
                for k in self.strip_keys:
                    msg.pop(k, None)
