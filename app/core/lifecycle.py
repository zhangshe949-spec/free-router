# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""模型生命周期管理器：自动发现/新增/下线/改名/免费转付费/fallback chain。"""
import asyncio
import re
import time
import httpx
import logging

from .registry import ModelEntry

logger = logging.getLogger("free_router.lifecycle")

# 免费转付费错误的正则（OpenRouter 风格）
_BILLING_RE = re.compile(
    r"use this slug instead:\s*<([^>]+)>", re.IGNORECASE)


class LifecycleManager:
    def __init__(self, cfg, registry, quota, providers):
        self.cfg = cfg
        self.registry = registry
        self.quota = quota
        self.providers = providers
        self._refresh_lock = asyncio.Lock()

    # ---------- 自动发现 ----------
    async def run_discovery_once(self, force=False):
        """拉取各提供商 /models，更新注册表。"""
        if self._refresh_lock.locked() and not force:
            return
        async with self._refresh_lock:
            urls = self.cfg.lifecycle.discovery_urls
            for provider, url in urls.items():
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        headers = {}
                        pcfg = self.cfg.providers.get(provider)
                        if pcfg and pcfg.resolved_key:
                            headers["Authorization"] = f"Bearer {pcfg.resolved_key}"
                        resp = await client.get(url, headers=headers)
                        if resp.status_code != 200:
                            logger.warning("发现失败 %s: HTTP %s", provider, resp.status_code)
                            continue
                        data = resp.json()
                        self._apply_discovery(provider, data)
                except Exception as e:
                    logger.error("发现异常 %s: %s", provider, e)

    def _apply_discovery(self, provider, data):
        """应用发现结果：新增/下线/改名。

        free_only 提供商（聚合网关如 Kilo）：只接纳免费档位模型（`:free` 后缀
        或 kilo-auto/* 自动路由），避免把付费模型纳入自动 fallback 池——用户要求
        付费模型只能手动切换。
        """
        pcfg = self.cfg.providers.get(provider)
        free_only = bool(getattr(pcfg, "free_only", False))
        static_ids = {m.get("id") for m in (pcfg.models if pcfg else [])}
        remote_ids = set()
        for item in data.get("data", []):
            mid = item.get("id")
            if not mid:
                continue
            remote_ids.add(mid)
            if self.registry.get(mid):
                continue
            if free_only and not self._is_free(mid) and mid not in static_ids:
                continue
            self.registry.add(mid, provider,
                              capability=self._infer_capability(mid),
                              tier=self._infer_tier(mid))
            logger.info("新增模型: %s (%s)", mid, provider)
        # 下线检测：注册表中该商家的、发现列表中不存在的
        for m in self.registry.list_active():
            if m.provider == provider and m.id not in remote_ids:
                self.registry.remove(m.id)
                logger.info("下线模型: %s (%s)", m.id, provider)

    @staticmethod
    def _is_free(mid: str) -> bool:
        """判定聚合网关的免费档位：`:free` 标记 / kilo-auto/free。"""
        ml = mid.lower()
        return ":free" in ml or ml == "kilo-auto/free"

    def _infer_capability(self, mid) -> dict:
        """新模型冷启动：按名称/ID 启发式推断能力标签。"""
        mid_l = mid.lower()
        cap = {"score": 50, "code": 50, "reason": 50, "long": 50, "vision": False}
        if any(v in mid_l for v in ("vl", "vision", "gemma-3", "llava", "internvl")):
            cap["vision"] = True
        if any(v in mid_l for v in ("qwen", "deepseek", "llama", "glm")):
            cap["score"] = 65
        if "flash" in mid_l or "mini" in mid_l:
            cap["long"] = 40   # 快速小模型长文本弱
        if "turbo" in mid_l or "instant" in mid_l:
            cap["score"] += 5
        return cap

    @staticmethod
    def _infer_tier(mid: str) -> str:
        """质量分级（用户策略：高质量优先、小模型兜底）：
        high=旗舰（ultra/max/omni 或参数量>=100b），low=明确小模型
        （mini/nano/lite/micro 或参数量<=9b），其余 medium。
        参数量解析用边界正则，避免 27b 误配 7b、a12b 误配 12b 之外的问题。"""
        import re as _re
        ml = mid.lower()
        sizes = [float(x) for x in
                 _re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![\d])", ml)]
        if any(k in ml for k in ("ultra", "-max", "max-", "omni", "2.4t")):
            return "high"
        if sizes and max(sizes) >= 100:
            return "high"
        if any(k in ml for k in ("mini", "nano", "micro", "lite", "small", "-s-")):
            return "low"
        if sizes and max(sizes) <= 9:
            return "low"
        return "medium"

    # ---------- 免费转付费处理 ----------
    def handle_billing_error(self, error_text: str):
        """解析免费转付费错误，返回建议的替代 slug（若可解析）。"""
        m = _BILLING_RE.search(error_text)
        if m:
            return m.group(1).strip()
        return None

    # ---------- fallback chain ----------
    def fallback_chain(self, mid: str) -> list:
        """模型组内降级链：当前 -> 同系列下一档 -> 上一代稳定版。
        简化实现：返回注册表中同 provider 的其他可用模型，按能力降序。"""
        cur = self.registry.get(mid)
        if not cur:
            return []
        peers = [m for m in self.registry.list_active()
                 if m.provider == cur.provider and m.id != mid]
        peers.sort(key=lambda x: -x.capability.get("score", 0))
        return [m.id for m in peers]


# 周期性刷新任务
async def periodic_refresh(lifecycle: LifecycleManager):
    interval = lifecycle.cfg.lifecycle.refresh_interval_hours * 3600
    while True:
        await asyncio.sleep(interval)
        await lifecycle.run_discovery_once()