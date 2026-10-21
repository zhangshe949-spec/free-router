# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""插件框架（P9）：动态加载插件 + 请求生命周期钩子。

插件通过 config.yaml 的 plugins 段配置：
    plugins:
      - name: request-logger
        module: app.plugins.builtin
        class: RequestLoggerPlugin
        config: {}

加载失败只告警、不中断启动（单个插件故障不影响主流程）。
"""
from __future__ import annotations

import importlib
import logging

from .base import BasePlugin

logger = logging.getLogger("free_router.plugins")


def load_plugins(cfg) -> list:
    """按 cfg.plugins 动态 import module.class 并实例化。

    返回插件实例列表（按配置顺序）。加载失败的条目跳过并告警。
    """
    plugins = []
    for spec in (getattr(cfg, "plugins", None) or []):
        if not isinstance(spec, dict):
            continue
        module_name = spec.get("module")
        class_name = spec.get("class")
        if not module_name or not class_name:
            logger.warning("插件配置缺 module/class，已跳过: %r", spec)
            continue
        try:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)
            inst = cls(spec.get("config") or {})
            plugins.append(inst)
            logger.info("插件已加载: %s.%s", module_name, class_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("插件加载失败 %s.%s: %s", module_name, class_name, e)
    return plugins


__all__ = ["BasePlugin", "load_plugins"]
