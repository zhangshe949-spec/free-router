# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Kilo 网关提供商适配器（免 key）"""
from . import KiloGWAdapter

# 复用基类
class KiloProvider(KiloGWAdapter):
    name = "kilogw"