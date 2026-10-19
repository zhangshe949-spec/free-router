"""Kilo 网关提供商适配器（免 key）"""
from . import KiloGWAdapter

# 复用基类
class KiloProvider(KiloGWAdapter):
    name = "kilogw"