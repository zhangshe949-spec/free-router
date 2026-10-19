"""模型生命周期管理器：自动发现/新增/下线/改名/免费转付费/fallback chain。"""
import asyncio
import json
import re
import time
import httpx
import logging
from pathlib import Path

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
        # 事件回调（main.py 注入 Notifier.add）→ 新增/下线模型时推通知
        self.on_event = None

    # ---------- 变更通知（与上次快照对比） ----------
    def _snapshot_file(self) -> Path:
        return Path(self.cfg.quota.db_path).parent / "model_snapshot.json"

    def _current_set(self) -> dict:
        out = {}
        for m in self.registry.list_active():
            out[f"{m.provider}/{m.id}"] = {
                "provider": m.provider, "model": m.id,
                "free": (getattr(m, "type", "free") == "free"),
                "tier": getattr(m, "tier", "medium"),
                "tags": list(getattr(m, "tags", []) or []),
            }
        return out

    def _snapshot_and_notify(self) -> None:
        """把本次发现的模型集合与上次快照对比，只对真实变化发通知。

        这样既避免「每次重启刷一屏新增」，又能在模型库真的更新时提示用户
        （尤其免费模型），由用户决定是否保留。
        """
        cur = self._current_set()
        prev = None
        try:
            f = self._snapshot_file()
            if f.exists():
                prev = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            prev = None
        try:
            self._snapshot_file().write_text(
                json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug("快照写入失败: %s", e)
        if not isinstance(prev, dict) or not prev:
            return                      # 首次运行：只建立基线
        if not self.on_event:
            return
        added = [v for k, v in cur.items() if k not in prev]
        removed = [v for k, v in prev.items() if k not in cur]
        for info in added:
            self.on_event("model_added", info.get("provider", ""), info.get("model", ""),
                          free=bool(info.get("free", True)),
                          tier=info.get("tier", "medium"),
                          tags=list(info.get("tags", []) or []))
        for info in removed:
            self.on_event("model_removed", info.get("provider", ""), info.get("model", ""),
                          free=bool(info.get("free", True)),
                          tags=list(info.get("tags", []) or []))
        if added or removed:
            logger.info("模型库变更：新增 %d，下线 %d", len(added), len(removed))

    # ---------- 自动发现 ----------
    async def run_discovery_once(self, force=False, provider_filter=None):
        """拉取各提供商 /models，更新注册表。"""
        if self._refresh_lock.locked() and not force:
            return
        async with self._refresh_lock:
            urls = self.cfg.lifecycle.discovery_urls
            for provider, url in urls.items():
                if provider_filter and provider != provider_filter:
                    continue
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
            # 首次发现完成 → 之后的增删才产生通知（首轮只是灌数据）
            self._snapshot_and_notify()

    def _apply_discovery(self, provider, data):
        """应用发现结果：新增/下线/更新。

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
            meta = self._extract_metadata(item)
            existing = self.registry.get(mid)
            if existing and existing.status == "active":
                # 同时存在：检查版本/元信息是否变化 -> 仅记录通知，不重新加载
                if self._meta_changed(existing, meta, item):
                    self._notify("model_updated", provider, mid,
                                free=(getattr(existing, "type", "free") == "free"),
                                tier=existing.tier,
                                tags=list(getattr(existing, "tags", []) or []))
                    # 更新元信息（仅展示用途，不影响路由权重）
                    self._apply_meta_update(existing, meta)
                continue
            if self.registry.get(mid):
                continue
            if free_only and not self._is_free(mid) and mid not in static_ids:
                continue
            tags = self._infer_tags(mid)
            tier = self._infer_tier(mid)
            cap = self._infer_capability(mid)
            added = self.registry.add(mid, provider,
                                      capability=cap,
                                      tier=tier, tags=tags,
                                      **meta)
            if not added:
                continue
            logger.info("新增模型: %s (%s)", mid, provider)
            self._notify("model_added", provider, mid,
                         free=True, tier=tier, tags=tags)
        # 下线检测：注册表中该商家的、发现列表中不存在的
        for m in self.registry.list_active():
            if m.provider == provider and m.id not in remote_ids:
                self.registry.remove(m.id)
                logger.info("下线模型: %s (%s)", m.id, provider)
                self._notify("model_removed", provider, m.id,
                             free=(getattr(m, "type", "free") == "free"),
                             tags=list(getattr(m, "tags", []) or []))

    def _meta_changed(self, existing, meta, raw_item):
        """检查远程模型元信息是否与本地注册表不一致（版本/参数/价格变化）。"""
        if int(getattr(existing, "context_window", 0) or 0) != int(meta.get("context_window", 0) or 0):
            return True
        if int(getattr(existing, "max_output_tokens", 0) or 0) != int(meta.get("max_output_tokens", 0) or 0):
            return True
        old_pricing = getattr(existing, "pricing_per_1k_tokens", {}) or {}
        new_pricing = meta.get("pricing_per_1k_tokens", {}) or {}
        if old_pricing != new_pricing:
            return True
        old_params = set(getattr(existing, "supported_parameters", []) or [])
        new_params = set(meta.get("supported_parameters", []) or [])
        if old_params != new_params:
            return True
        return False

    def _apply_meta_update(self, existing, meta):
        """更新已存在模型的元信息（仅展示用途，不影响路由权重）。"""
        changed = False
        if meta.get("context_window") is not None:
            v = int(meta["context_window"])
            if getattr(existing, "context_window", 0) != v:
                existing.context_window = v
                changed = True
        if meta.get("max_output_tokens") is not None:
            v = int(meta["max_output_tokens"])
            if getattr(existing, "max_output_tokens", 0) != v:
                existing.max_output_tokens = v
                changed = True
        if meta.get("pricing_per_1k_tokens") is not None:
            if getattr(existing, "pricing_per_1k_tokens", {}) != meta["pricing_per_1k_tokens"]:
                existing.pricing_per_1k_tokens = meta["pricing_per_1k_tokens"]
                changed = True
        if meta.get("supported_parameters") is not None:
            if list(getattr(existing, "supported_parameters", []) or []) != list(meta["supported_parameters"]):
                existing.supported_parameters = list(meta["supported_parameters"])
                changed = True
        if changed:
            existing.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.registry.save(existing)

    def _notify(self, kind, provider, model, **extra):
        """统一通知入口：仅在 on_event 已注入时发送（展示用，不影响模型正常使用）。"""
        if self.on_event:
            self.on_event(kind, provider, model, **extra)

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
    def _extract_metadata(item: dict) -> dict:
        """从发现结果里抽取模型元信息（context_window / max_output_tokens /
        pricing / supported_parameters），用于丰富注册表条目。

        兼容 OpenRouter/Kilo 风格的字段名（context_length / pricing.prompt）以及
        更直白的命名（context_window / max_tokens / supported_parameters）。
        """
        out = {}
        cw = (item.get("context_length") or item.get("context_window")
              or item.get("max_context_length"))
        if cw is not None:
            try:
                out["context_window"] = int(cw)
            except (TypeError, ValueError):
                pass
        mot = (item.get("max_output_tokens") or item.get("max_tokens")
               or item.get("max_completion_tokens"))
        if mot is not None:
            try:
                out["max_output_tokens"] = int(mot)
            except (TypeError, ValueError):
                pass
        pr = item.get("pricing") or item.get("pricing_per_1k_tokens")
        if isinstance(pr, dict):
            norm = {}
            for src, dst in (("prompt", "input"), ("input", "input"),
                             ("completion", "output"), ("output", "output")):
                v = pr.get(src)
                if v is not None:
                    norm[dst] = v
            if norm:
                out["pricing_per_1k_tokens"] = norm
        sp = item.get("supported_parameters")
        if isinstance(sp, list):
            out["supported_parameters"] = list(sp)
        return out

    # 能力标签规则（与前端 CAP_RULES 对齐，供任务感知选择器与 UI 复用）
    _TAG_RULES = [
        ("vision", re.compile(r"(^|[-_/])(vl|vision|4v|omni|multimodal|image|visual)([-_/.]|$)|(internvl|llava|gemma-3|minicpm-v)", re.I)),
        ("audio", re.compile(r"(audio|speech|tts|asr|whisper|voice|realtime)", re.I)),
        ("embedding", re.compile(r"(embed|bge|gte|m3e|text-embedding)", re.I)),
        ("rerank", re.compile(r"rerank", re.I)),
        ("code", re.compile(r"(coder|code|devstral|starcoder|codegen)", re.I)),
        ("math", re.compile(r"(math|qwq)", re.I)),
        ("reasoning", re.compile(r"(^|[-_/])(r1|reason|reasoner|think|qwq|o1|o3|o4|zeta)([-_/.]|$)", re.I)),
        ("long", re.compile(r"(128k|200k|256k|512k|1m|long|32k)", re.I)),
        ("fast", re.compile(r"(flash|lite|turbo|mini|small|air|speed|nano|tiny)", re.I)),
        ("flagship", re.compile(r"(max|pro|plus|ultra|large|opus|sonnet)", re.I)),
    ]

    @classmethod
    def _infer_tags(cls, mid: str) -> list:
        """按模型 ID 推断能力标签，供任务感知选择器与前端复用。"""
        return [t for t, rx in cls._TAG_RULES if rx.search(mid or "")]

    def backfill_metadata(self) -> int:
        """补齐已入库模型的 tags / capability（历史数据升级，无需清库）。"""
        n = 0
        for m in self.registry.list_active():
            changed = False
            if not (getattr(m, "tags", None) or []):
                tags = self._infer_tags(m.id)
                if tags:
                    m.tags = tags
                    changed = True
            if not (m.capability or {}):
                m.capability = self._infer_capability(m.id)
                changed = True
            if changed:
                self.registry.save(m)
                n += 1
        if n:
            logger.info("元数据回填：更新 %d 个模型的能力标签", n)
        return n

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