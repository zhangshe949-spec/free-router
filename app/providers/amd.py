# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""AMD Radeon 提供商适配器（国内直连，rc- key）"""
from . import OpenAICompatAdapter


class AMDAdapter(OpenAICompatAdapter):
    name = "amd-radeon"