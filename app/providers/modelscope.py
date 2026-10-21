# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""ModelScope 魔塔提供商适配器（key 独立配置）"""
from . import OpenAICompatAdapter


class ModelScopeAdapter(OpenAICompatAdapter):
    name = "modelscope"