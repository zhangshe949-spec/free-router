"""可插拔策略（P7）：把“打分/排序”开放给外部实现。

配置：
    router:
      policy:
        module: my_pkg.my_policies     # 可导入的 Python 模块
        class: WeightedPolicy          # 类名
        args: {weight_code: 2.0}       # 可选，构造参数

外部策略只需实现：
    def score(self, entry, ctx: dict, base: float) -> float

另有选择策略（决定“下一跳”而不是打分）：
    def pick(self, entries: list, ctx: dict) -> Optional[entry]

未配置或加载失败 → 使用内置算法（ModelSelector.score），绝不影响可用性。
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

logger = logging.getLogger("free_router.policy")


class BuiltinPolicy:
    """内置策略：不做额外打分（由 ModelSelector 内置公式负责）。"""

    name = "builtin"

    def score(self, entry, ctx: dict, base: float) -> float:
        return float(base)


def load_policy(cfg) -> Any:
    """按配置加载策略对象；失败时回退 BuiltinPolicy。"""
    spec = getattr(getattr(cfg, "router", None), "policy", None) or {}
    module_name = spec.get("module")
    class_name = spec.get("class")
    if not module_name or not class_name:
        return BuiltinPolicy()
    try:
        mod = importlib.import_module(str(module_name))
        cls = getattr(mod, str(class_name))
        obj = cls(**(spec.get("args") or {}))
        logger.info("已加载自定义策略 %s.%s", module_name, class_name)
        return obj
    except Exception as e:
        logger.warning("策略加载失败 %s.%s (%s) → 回退内置策略",
                       module_name, class_name, e)
        return BuiltinPolicy()


def make_policy(module_name: str, class_name: str, args: Optional[dict] = None) -> Any:
    """运行期热加载（供 /admin/policy 使用）。"""
    mod = importlib.import_module(str(module_name))
    cls = getattr(mod, str(class_name))
    return cls(**(args or {}))