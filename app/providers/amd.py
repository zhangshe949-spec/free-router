"""AMD Radeon 提供商适配器（国内直连，rc- key）"""
from . import OpenAICompatAdapter


class AMDAdapter(OpenAICompatAdapter):
    name = "amd-radeon"