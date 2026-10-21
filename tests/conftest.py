# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""pytest 全局配置：把测试与开发机的真实凭据彻底隔离。

为什么需要：路由层会把「需要 key 但未配置的提供商」直接跳过（省一次 failover）。
如果测试继承了开发机 ~/.free-router/.env 里的真实 key，测试就依赖本机环境：
- 在有 key 的机器上通过；
- 在没有 key 的 CI（Linux runner）上所有提供商被判为未配置 → 候选池为空
  → AllExhausted，测试全红。

这里做两件事：
1. 收集阶段清空所有提供商相关的环境变量；
2. 提供 no_env_files 夹具：load_config(..., load_env_files=False)，
   让配置加载完全不读 .env 文件。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 与 config/config.yaml 里 providers.*.key_env 对应的全部候选名
_PROVIDER_KEY_ENV_NAMES = [
    "RADEON_API_KEY", "AMD_API_KEY",
    "MODELSCOPE_API_KEY", "HERMES_CUSTOM_MODELSCOPE_API_KEY",
    "KILOCODE_API_KEY", "KILO_API_KEY", "KILOGW_API_KEY",
    "ZAI_API_KEY", "SILICONFLOW_API_KEY", "QIANFAN_API_KEY",
    "HUNYUAN_API_KEY", "SPARK_API_KEY", "DASHSCOPE_API_KEY",
    "ARK_API_KEY", "DEEPSEEK_API_KEY",
    "ARK_API_KEY_2", "RADEON_API_KEY_2",
    "MODELSCOPE_API_KEY_2", "MODELSCOPE_API_KEY_3",
]

# 收集阶段即清空（早于任何 fixture / 模块级 load_config）
for _name in _PROVIDER_KEY_ENV_NAMES:
    os.environ.pop(_name, None)


@pytest.fixture
def clean_env(monkeypatch):
    """确保当前测试期间没有任何提供商凭据泄漏进环境。"""
    for name in _PROVIDER_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def no_env_files():
    """标记：load_config 应跳过 .env 加载（防止读到开发机真实密钥）。"""
    return True


@pytest.fixture
def keyless():
    """把指定提供商声明为「免 key 可用」，让测试自洽而不依赖真实凭据。

    路由层会跳过「需要 key 但未配置」的提供商。测试若依赖开发机环境变量里
    的真 key，在无凭据的 CI 上就会因候选池为空而失败。正确做法是显式声明：
      - 需要该提供商可用 → keyless(cfg, "amd-radeon")
      - 需要它被跳过     → 设一个必然不存在的 key_env
    """
    def _apply(cfg, *names):
        for name in names:
            pc = cfg.providers.get(name)
            if pc is not None:
                pc.key_env = ""
                pc.api_key = "test-key"
        return cfg
    return _apply


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """自动夹具：每个测试开始前都清空提供商凭据。"""
    for name in _PROVIDER_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
