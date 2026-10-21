# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Phase 1：7 家国内免费提供商配置/工厂测试（无真实网络）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.core.config import load_config
from app.core.registry import Registry
from app.providers import get_provider_pool, OpenAICompatAdapter
from app.providers.amd import AMDAdapter
from app.providers.kilogw import KiloProvider

CFG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"

NEW = ["zhipu", "siliconflow", "qianfan", "hunyuan", "spark", "dashscope", "ark"]


@pytest.fixture
def cfg():
    return load_config(CFG_PATH, load_env_files=False)


def test_seven_new_providers_present(cfg):
    for name in NEW:
        assert name in cfg.providers, f"缺少提供商 {name}"
        assert cfg.providers[name].base_url.startswith("http")
        assert cfg.providers[name].key_env, f"{name} 应配置 key_env"


def test_provider_pool_covers_all_and_is_openai_compat(cfg):
    pool = get_provider_pool(cfg)
    assert set(pool.keys()) == set(cfg.providers.keys())
    for name in NEW:
        assert isinstance(pool[name], OpenAICompatAdapter), f"{name} 应走 OpenAI 兼容适配器"
    # 既有特殊适配器仍正确
    assert isinstance(pool["amd-radeon"], AMDAdapter)
    assert isinstance(pool["kilogw"], KiloProvider)


def test_new_provider_models_registered(cfg, tmp_path):
    reg = Registry(cfg, db_path=tmp_path / "p1.db")
    ids = {m.id for m in reg.list_active()}
    for mid in ("glm-4-flash", "glm-4.7-flash", "Qwen/Qwen2.5-7B-Instruct",
                "ernie-speed-8k", "ernie-lite-8k", "hunyuan-lite",
                "spark-lite", "qwen-plus", "qwen-turbo",
                # ark：只用实测「已开通」的带日期后缀确切 id（别名会 ModelNotOpen）
                "deepseek-v4-1-flash-260910", "deepseek-v4-pro-ga-260813",
                "doubao-seed-2-1-pro-260915"):
        assert mid in ids, f"应注册模型 {mid}"
    assert reg.get("glm-4-flash").provider == "zhipu"
    assert reg.get("qwen-plus").provider == "dashscope"


def test_new_models_have_capability(cfg, tmp_path):
    reg = Registry(cfg, db_path=tmp_path / "p2.db")
    for m in reg.list_active():
        if m.provider in NEW:
            cap = m.capability
            for k in ("score", "code", "reason", "long", "vision"):
                assert k in cap, f"{m.id} 缺少能力维度 {k}"


def test_discovery_urls_present(cfg):
    # ark 故意不放进自动发现（其 /models 混有图像/视频/3D 生成模型，
    # 自动加会把非对话模型灌进候选池）；其对话模型在 config 里手工维护。
    for name in NEW:
        if name == "ark":
            continue
        url = cfg.lifecycle.discovery_urls.get(name, "")
        assert url.endswith("/models"), f"{name} 缺少 /models 发现地址"


def test_api_keys_still_empty(cfg):
    for name in NEW:
        assert (cfg.providers[name].api_key or "") == ""