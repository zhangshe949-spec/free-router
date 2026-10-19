"""ModelScope 魔塔提供商适配器（key 独立配置）"""
from . import OpenAICompatAdapter


class ModelScopeAdapter(OpenAICompatAdapter):
    name = "modelscope"