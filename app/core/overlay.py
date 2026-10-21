# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""运行时覆盖层（P1）：把“动态增删的提供商/模型”写进独立文件，不动 config.yaml。

好处：config.yaml 仍可手工维护注释；运行期改动落在
`config/providers.local.yaml`（可用 FREE_ROUTER_OVERLAY 覆盖路径），
重启后 load_config 自动合并。

结构：
    providers:
      my-gw:
        base_url: https://api.example.com/v1
        type: free                # free | paid（该商默认档位）
        key_env: MY_GW_KEY
        free_only: true
        models:
          - {id: xxx, type: free, tags: [reasoning], tier: high, priority: 1}
    disabled:                     # 墓碑：从静态清单里剔除的模型
      - {provider: kilogw, model: openai/gpt-6.1-sol}
"""
from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_NAME = "providers.local.yaml"


def overlay_path(cfg_path: Path) -> Path:
    import os
    env = os.environ.get("FREE_ROUTER_OVERLAY")
    if env:
        return Path(env).expanduser()
    return Path(cfg_path).parent / DEFAULT_NAME


def load_overlay(cfg_path: Path) -> Dict[str, Any]:
    p = overlay_path(cfg_path)
    if not p.exists():
        return {"providers": {}, "disabled": []}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"providers": {}, "disabled": []}
    data.setdefault("providers", {})
    data.setdefault("disabled", [])
    return data


def save_overlay(cfg_path: Path, data: Dict[str, Any]) -> Path:
    p = overlay_path(cfg_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p


# ---------- 变更操作（读-改-写） ----------
def add_provider(cfg_path: Path, name: str, base_url: str, **kw) -> Path:
    data = load_overlay(cfg_path)
    block = data["providers"].setdefault(name, {})
    block["base_url"] = base_url
    for k in ("type", "key_env", "free_only", "tier_quality",
              "request_timeout", "extra_headers"):
        if kw.get(k) is not None:
            block[k] = kw[k]
    block.setdefault("models", [])
    return save_overlay(cfg_path, data)


def remove_provider(cfg_path: Path, name: str) -> Path:
    data = load_overlay(cfg_path)
    data["providers"].pop(name, None)
    return save_overlay(cfg_path, data)


def add_model(cfg_path: Path, provider: str, model: str, **kw) -> Path:
    data = load_overlay(cfg_path)
    block = data["providers"].setdefault(provider, {})
    if kw.get("base_url"):
        block.setdefault("base_url", kw["base_url"])
    models: List[dict] = block.setdefault("models", [])
    entry = {"id": model}
    for k in ("type", "tags", "tier", "priority", "capability"):
        if kw.get(k) is not None:
            entry[k] = kw[k]
    models[:] = [m for m in models if m.get("id") != model] + [entry]
    # 若曾被禁用，解除墓碑
    data["disabled"] = [d for d in data["disabled"]
                        if not (d.get("provider") == provider and d.get("model") == model)]
    return save_overlay(cfg_path, data)


def remove_model(cfg_path: Path, provider: str, model: str) -> Path:
    data = load_overlay(cfg_path)
    block = data["providers"].get(provider)
    if block and block.get("models"):
        block["models"] = [m for m in block["models"] if m.get("id") != model]
    disabled = data.setdefault("disabled", [])
    if not any(d.get("provider") == provider and d.get("model") == model
               for d in disabled):
        disabled.append({"provider": provider, "model": model})
    return save_overlay(cfg_path, data)


def disable_model(cfg_path: Path, provider: str, model: str) -> Path:
    return remove_model(cfg_path, provider, model)