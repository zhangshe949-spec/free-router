# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""额度监控引擎：RPM/RPD/TPM/TPD 四维 + 分级冷却 + SQLite 持久化 + 多租户 + 额度预测。

从提供商错误体学习真实限制（而非仅硬编码）。

Phase 0 修复：用量写穿（write-through）——原实现仅在进程关停时 persist()，
崩溃/强杀会丢失全部内存用量。现引入 dirty 标记 + 阈值/定时 checkpoint。

P3：额度预测 —— 按近期请求速率预估剩余额度还能撑多久，低于阈值则提前把该模型
移出候选（软过滤，无冷却态；速率窗口自然衰减后自动恢复）。

P4：多租户 —— usage/cooldown 主键加入 tenant 维（默认 "default"），旧库自动迁移。

P8：多 Key 故障转移 —— usage/cooldown 主键加入 key_hash 维，
Key 级错误（401/403/429）仅标记该 Key 冷却，不影响同提供商其他 Key；
Provider 级错误（5xx/timeout）触发提供商级故障转移。
"""
import asyncio
import logging
import sqlite3
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger("free_router.quota")

DEFAULT_TENANT = "default"

# Key 级错误分类
KEY_LEVEL_ERRORS = frozenset(("401", "403", "429", "key_unhealthy"))
PROVIDER_LEVEL_ERRORS = frozenset(("5xx", "timeout"))
MODEL_LEVEL_ERRORS = frozenset(("model_not_found",))


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_ts() -> float:
    return time.time()


def _k(tenant, provider, model, key_hash="") -> tuple:
    """生成包含 key_hash 的复合键。空 key_hash 视为默认单 Key 模式。"""
    return (tenant or DEFAULT_TENANT, provider, model, key_hash or "")


_USAGE_COLS = """(tenant TEXT DEFAULT 'default', provider TEXT, model TEXT,
    key_hash TEXT, rpm_cnt INTEGER, rpm_window REAL, rpd_cnt INTEGER,
    rpd_day TEXT, tpm_tokens INTEGER, tpm_window REAL, tpd_tokens INTEGER,
    tpd_day TEXT, PRIMARY KEY(tenant, provider, model, key_hash))"""

_COOLDOWN_COLS = """(tenant TEXT DEFAULT 'default', provider TEXT, model TEXT,
    key_hash TEXT, reason TEXT, until REAL, retry_after INTEGER, hits INTEGER,
    PRIMARY KEY(tenant, provider, model, key_hash))"""


@dataclass
class Usage:
    rpm_cnt: int = 0
    rpm_window: float = 0.0
    rpd_cnt: int = 0
    rpd_day: str = ""
    tpm_tokens: int = 0
    tpm_window: float = 0.0
    tpd_tokens: int = 0
    tpd_day: str = ""


class QuotaEngine:
    def __init__(self, cfg, db_path: Path = None,
                 checkpoint_every: int = 20, checkpoint_interval: float = 10.0):
        self.cfg = cfg
        self.db_path = Path(db_path or "data/free_router.db")
        self._lock = threading.Lock()
        self._usage = defaultdict(Usage)        # (tenant,p,m,k) -> Usage
        self._cooldowns = {}                    # (tenant,p,m,k) -> dict
        self._recent = defaultdict(lambda: deque(maxlen=2000))  # P3 速率样本
        self._dirty = False
        self._pending = 0
        self._last_persist = time.time()
        self.checkpoint_every = max(1, int(checkpoint_every))
        self.checkpoint_interval = float(checkpoint_interval)
        self._init_db()
        self._load_from_db()

    # ---------- DB ----------
    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(f"CREATE TABLE IF NOT EXISTS usage {_USAGE_COLS}")
            c.execute(f"CREATE TABLE IF NOT EXISTS cooldown {_COOLDOWN_COLS}")
            self._migrate_legacy(c)

    def _migrate_legacy(self, c):
        """旧库（无 tenant 列）自动迁移：原数据归入 default 租户。"""
        for table in ("usage", "cooldown"):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if "tenant" in cols:
                continue
            legacy = f"{table}_legacy"
            c.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
            if table == "usage":
                c.execute(f"CREATE TABLE usage {_USAGE_COLS}")
                c.execute(f"""INSERT INTO usage
                    (tenant, provider, model, key_hash, rpm_cnt, rpm_window,
                     rpd_cnt, rpd_day, tpm_tokens, tpm_window, tpd_tokens, tpd_day)
                    SELECT 'default', provider, model, key_hash, rpm_cnt, rpm_window,
                           rpd_cnt, rpd_day, tpm_tokens, tpm_window, tpd_tokens, tpd_day
                    FROM {legacy}""")
            else:
                c.execute(f"CREATE TABLE cooldown {_COOLDOWN_COLS}")
                c.execute(f"""INSERT INTO cooldown
                    (tenant, provider, model, key_hash, reason, until, retry_after, hits)
                    SELECT 'default', provider, model, key_hash, reason, until,
                           retry_after, hits FROM {legacy}""")
            c.execute(f"DROP TABLE {legacy}")
            logger.info("额度库已迁移到多租户结构: %s", table)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _load_from_db(self):
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            for r in c.execute("SELECT * FROM usage"):
                u = Usage(rpm_cnt=r["rpm_cnt"], rpm_window=r["rpm_window"],
                          rpd_cnt=r["rpd_cnt"], rpd_day=r["rpd_day"],
                          tpm_tokens=r["tpm_tokens"], tpm_window=r["tpm_window"],
                          tpd_tokens=r["tpd_tokens"], tpd_day=r["tpd_day"])
                self._usage[(r["tenant"] or DEFAULT_TENANT, r["provider"],
                             r["model"], r["key_hash"])] = u
            for r in c.execute("SELECT * FROM cooldown"):
                self._cooldowns[(r["tenant"] or DEFAULT_TENANT, r["provider"],
                                 r["model"], r["key_hash"])] = {
                    "reason": r["reason"], "until": r["until"],
                    "retry_after": r["retry_after"], "hits": r["hits"],
                }

    def _write_db(self):
        """实际写库（并发一致性由调用方通过 _lock 保证）。"""
        with self._conn() as c:
            for (t, p, m, k), u in self._usage.items():
                c.execute("""INSERT OR REPLACE INTO usage
                    (tenant, provider, model, key_hash, rpm_cnt, rpm_window,
                     rpd_cnt, rpd_day, tpm_tokens, tpm_window, tpd_tokens, tpd_day)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (t, p, m, k, u.rpm_cnt, u.rpm_window, u.rpd_cnt, u.rpd_day,
                           u.tpm_tokens, u.tpm_window, u.tpd_tokens, u.tpd_day))
            for (t, p, m, k), cd in self._cooldowns.items():
                c.execute("""INSERT OR REPLACE INTO cooldown
                    (tenant, provider, model, key_hash, reason, until, retry_after, hits)
                    VALUES(?,?,?,?,?,?,?,?)""",
                          (t, p, m, k, cd["reason"], cd["until"],
                           cd["retry_after"], cd["hits"]))

    def _flush_locked(self):
        """在已持有 _lock 的情况下落库并清 dirty。"""
        self._write_db()
        self._dirty = False
        self._pending = 0
        self._last_persist = time.time()

    def persist(self):
        """显式全量落库（关停时调用；线程安全）。"""
        with self._lock:
            self._flush_locked()

    def persist_if_dirty(self) -> bool:
        """仅在有变更时落库；返回是否执行了写入（后台 checkpoint 用）。"""
        with self._lock:
            if not self._dirty:
                return False
            self._flush_locked()
            return True

    def _mark_dirty_locked(self):
        """标记有未落库变更；累计到阈值则立即 checkpoint（须持有 _lock）。"""
        self._dirty = True
        self._pending += 1
        if self._pending >= self.checkpoint_every:
            self._flush_locked()

    async def checkpoint_loop(self, interval: float = None):
        """后台定时 checkpoint：崩溃时最多丢 ~interval 秒的用量。"""
        interval = self.checkpoint_interval if interval is None else interval
        while True:
            await asyncio.sleep(interval)
            try:
                self.persist_if_dirty()
            except Exception as e:  # 后台任务不因写库异常而终止
                logger.warning("quota checkpoint 失败: %s", e)

    # ---------- 记录用量 ----------
    def record(self, provider, model, key_hash="", usage=None, tenant=DEFAULT_TENANT):
        """usage: OpenAI 响应中的 usage {prompt_tokens, completion_tokens} 或 None"""
        with self._lock:
            k = _k(tenant, provider, model, key_hash)
            u = self._usage[k]
            now = _utc_ts(); day = _utc_day()
            self._recent[k].append(now)          # P3：速率样本

            # RPM 滑动窗口
            if now - u.rpm_window > 60:
                u.rpm_window = now; u.rpm_cnt = 0
            u.rpm_cnt += 1
            # RPD
            if u.rpd_day != day:
                u.rpd_day = day; u.rpd_cnt = 0
            u.rpd_cnt += 1
            # TPM/TPD
            if usage:
                tok = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
                if now - u.tpm_window > 60:
                    u.tpm_window = now; u.tpm_tokens = 0
                u.tpm_tokens += tok
                if u.tpd_day != day:
                    u.tpd_day = day; u.tpd_tokens = 0
                u.tpd_tokens += tok

            self._mark_dirty_locked()

    def is_quota_exceeded(self, provider, model, key_hash="", tenant=DEFAULT_TENANT) -> bool:
        """额度判定（日/窗口感知）：跨天或窗口过期时先惰性归零再判定。

        关键修复：额度耗尽的模型会被过滤而不再被 record()，若归零只发生在
        record() 里，次日额度将永远无法恢复——与「额度隔天自动恢复」冲突。
        """
        with self._lock:
            k = _k(tenant, provider, model, key_hash)
            u = self._usage.get(k)
            if not u:
                return False
            now = _utc_ts(); day = _utc_day()
            dirty = False
            if u.rpd_day and u.rpd_day != day and u.rpd_cnt:
                u.rpd_cnt = 0; u.rpd_day = day; dirty = True
            if u.tpd_day and u.tpd_day != day and u.tpd_tokens:
                u.tpd_tokens = 0; u.tpd_day = day; dirty = True
            if now - u.rpm_window > 60 and u.rpm_cnt:
                u.rpm_cnt = 0; dirty = True
            if now - u.tpm_window > 60 and u.tpm_tokens:
                u.tpm_tokens = 0; dirty = True
            if dirty:
                self._mark_dirty_locked()
            c = self.cfg.quota
            if u.rpm_cnt >= c.rpm_limit: return True
            if u.rpd_cnt >= c.rpd_limit: return True
            if u.tpm_tokens >= c.tpm_limit: return True
            if u.tpd_tokens >= c.tpd_limit: return True
            return False

    # ---------- P3 额度预测 ----------
    def request_rate(self, provider, model, key_hash="", tenant=DEFAULT_TENANT) -> float:
        """近窗口请求速率（req/s）；样本不足返回 0。"""
        win = float(getattr(self.cfg.quota, "predict_window_seconds", 30))
        now = _utc_ts()
        dq = self._recent.get(_k(tenant, provider, model, key_hash))
        if not dq:
            return 0.0
        recent = [t for t in dq if now - t <= win]
        rate_window = len(recent) / max(win, 1.0) if len(recent) >= 3 else 0.0
        with self._lock:
            u = self._usage.get(_k(tenant, provider, model, key_hash))
            rate_rpm = (u.rpm_cnt / 60.0) if u else 0.0
        # 取两者较大：窗口内突发的短时高频也能被感知（避免被采样上限低估）
        return max(rate_window, rate_rpm)

    def predict_exhausted(self, provider, model, key_hash="",
                          tenant=DEFAULT_TENANT) -> bool:
        """按当前速率预估剩余额度可支撑时长，低于阈值 → 提前避开（软过滤，自愈）。"""
        rate = self.request_rate(provider, model, key_hash, tenant)
        if rate <= 0:
            return False
        with self._lock:
            u = self._usage.get(_k(tenant, provider, model, key_hash))
            used = u.rpd_cnt if u else 0
        remaining = max(0, int(self.cfg.quota.rpd_limit) - used)
        need = float(getattr(self.cfg.quota, "predict_threshold_seconds", 20))
        return (remaining / rate) < need

    # ---------- 冷却 ----------
    def in_cooldown(self, provider, model, key_hash="", tenant=DEFAULT_TENANT) -> bool:
        k = _k(tenant, provider, model, key_hash)
        cd = self._cooldowns.get(k)
        if not cd: return False
        if _utc_ts() < cd["until"]: return True
        with self._lock:
            self._cooldowns.pop(k, None)   # 到期清除
            self._mark_dirty_locked()
        return False

    def purge_expired(self) -> int:
        """主动清除已到期冷却（巡检用：模型一恢复就立即回到候选池）。"""
        now = _utc_ts()
        removed = 0
        with self._lock:
            for k in [k for k, v in self._cooldowns.items() if v.get("until", 0) <= now]:
                self._cooldowns.pop(k, None)
                removed += 1
            if removed:
                self._mark_dirty_locked()
        return removed

    def cooled_count(self, models) -> int:
        return sum(1 for m in models if self.in_cooldown(m.provider, m.id))

    def apply_cooldown(self, provider, model, reason, key_hash="",
                       retry_after=None, tenant=DEFAULT_TENANT):
        """分级冷却：429 递增，5xx 固定，401/403 标记不健康，model_not_found 永久

        Key 级错误（401/403/429）仅冷却该 key_hash；
        Provider 级错误（5xx/timeout）冷却该 provider 下所有 key（通过空 key_hash 标记）；
        Model 级错误（model_not_found）永久冷却。
        """
        with self._lock:
            # Provider 级错误：标记空 key_hash，表示整个 provider 不可用
            provider_level = reason in PROVIDER_LEVEL_ERRORS
            target_key = "" if provider_level else key_hash

            k = _k(tenant, provider, model, target_key)
            cd = self._cooldowns.get(k, {"reason": reason, "until": 0,
                                         "retry_after": 0, "hits": 0})
            cd["hits"] += 1
            cd["reason"] = reason
            c = self.cfg.quota

            if reason == "429":
                # 递增：60 -> 300 -> 1800
                levels = [c.cooldown_429] + list(c.cooldown_429_escalate)
                idx = min(cd["hits"] - 1, len(levels) - 1)
                cd["retry_after"] = levels[idx]
            elif reason in ("5xx", "timeout"):
                cd["retry_after"] = c.cooldown_5xx
            elif reason == "model_not_found":
                cd["retry_after"] = 10**9   # 永久（直到自动发现确认恢复）
            elif reason in ("401", "403"):
                cd["retry_after"] = 10**9   # 密钥不健康，不自动重试
                cd["reason"] = "key_unhealthy"
            else:
                cd["retry_after"] = 60

            if retry_after:                 # 上游 Retry-After 更保守时取较大值
                try:
                    cd["retry_after"] = max(int(cd["retry_after"]), int(retry_after))
                except Exception:
                    pass
            cd["until"] = _utc_ts() + cd["retry_after"]
            self._cooldowns[k] = cd
            self._mark_dirty_locked()

            # 如果是 Key 级错误，检查该 provider/model 是否还有可用 Key
            if not provider_level:
                self._check_provider_key_health(tenant, provider, model)

    def _check_provider_key_health(self, tenant, provider, model):
        """检查 provider/model 是否还有健康 Key，若无则标记 provider 级冷却。"""
        # 获取该 provider/model 下所有 key_hash 的冷却状态
        has_healthy_key = False
        for (t, p, m, kh), cd in self._cooldowns.items():
            if t == tenant and p == provider and m == model and kh:
                if _utc_ts() >= cd["until"] and cd["reason"] not in KEY_LEVEL_ERRORS:
                    has_healthy_key = True
                    break
        if not has_healthy_key:
            # 所有 Key 都不健康，标记 provider 级冷却（空 key_hash）
            pk = _k(tenant, provider, model, "")
            if pk not in self._cooldowns or _utc_ts() >= self._cooldowns[pk]["until"]:
                self._cooldowns[pk] = {
                    "reason": "all_keys_exhausted",
                    "until": _utc_ts() + 300,  # 5 分钟后重试检查 Key 是否恢复
                    "retry_after": 300,
                    "hits": 0
                }
                self._mark_dirty_locked()

    def force_cooldown(self, provider, model, reason, tenant=DEFAULT_TENANT):
        self.apply_cooldown(provider, model, reason, tenant=tenant)

    def reset_all_cooldowns(self) -> int:
        with self._lock:
            n = len(self._cooldowns)
            self._cooldowns.clear()
            self._mark_dirty_locked()
        return n

    def clear_cooldown(self, provider, model, key_hash="", tenant=DEFAULT_TENANT) -> bool:
        """手动解除单个模型的冷却（运维用）。"""
        with self._lock:
            k = _k(tenant, provider, model, key_hash)
            hit = self._cooldowns.pop(k, None) is not None
            if hit:
                self._mark_dirty_locked()
            return hit

    # ---------- 从错误体学习限制 ----------
    def learn_from_error(self, provider, model, error, key_hash="",
                         tenant=DEFAULT_TENANT) -> str:
        """解析提供商错误，返回分类 reason，并应用冷却。"""
        reason = "unknown"
        status = getattr(error, "status_code", None) or getattr(error, "code", None)
        detail = str(getattr(error, "message", error))

        if status == 429 or "rate" in detail.lower() or "limit" in detail.lower():
            reason = "429"
            ra = getattr(error, "retry_after", None)
            self.apply_cooldown(provider, model, "429", key_hash, ra, tenant=tenant)
        elif status in (500, 502, 503, 504) or "timeout" in detail.lower():
            reason = "5xx"
            self.apply_cooldown(provider, model, "5xx", key_hash, tenant=tenant)
        elif status in (401, 403):
            reason = "401"
            self.apply_cooldown(provider, model, "401", key_hash, tenant=tenant)
        elif status == 404 or "model" in detail.lower():
            reason = "model_not_found"
            self.apply_cooldown(provider, model, "model_not_found", key_hash, tenant=tenant)
        else:
            self.apply_cooldown(provider, model, "unknown", key_hash, 30, tenant=tenant)
        return reason

    # ---------- 快照 ----------
    @staticmethod
    def _label(tenant, provider, model) -> str:
        return f"{provider}/{model}" if tenant in (None, "", DEFAULT_TENANT) \
            else f"{tenant}:{provider}/{model}"

    def snapshot(self, tenant: str = None) -> dict:
        with self._lock:
            items = list(self._usage.items())
            if tenant:
                items = [x for x in items if x[0][0] == tenant]
            return {
                "usage": {self._label(t, p, m): {
                    "rpm": u.rpm_cnt, "rpd": u.rpd_cnt,
                    "tpm": u.tpm_tokens, "tpd": u.tpd_tokens}
                    for (t, p, m, _), u in items},
                "cooldowns_active": sum(1 for cd in self._cooldowns.values()
                                        if _utc_ts() < cd["until"]),
            }

    def cooldown_snapshot(self, tenant: str = None) -> dict:
        out = {}
        for (t, p, m, _), cd in self._cooldowns.items():
            if _utc_ts() >= cd["until"]:
                continue
            if tenant and t != tenant:
                continue
            out[self._label(t, p, m)] = {
                "reason": cd["reason"], "until": cd["until"], "hits": cd["hits"],
                "retry_after": cd["retry_after"]}
        return out

    def prediction_snapshot(self) -> dict:
        """当前因「预计即将耗尽」被软过滤的模型。"""
        out = {}
        for (t, p, m) in {k[:3] for k in list(self._usage.keys())}:
            if self.predict_exhausted(p, m, tenant=t):
                out[self._label(t, p, m)] = {
                    "rate_per_s": round(self.request_rate(p, m, tenant=t), 3)}
        return out

    # ---------- Autofree 运行时统计查询 ----------
    def get_model_stats(self, provider: str, model: str,
                        tenant: str = DEFAULT_TENANT) -> dict:
        """返回单个模型的运行时统计（供 Autofree 评分用）"""
        with self._lock:
            u = self._usage.get(_k(tenant, provider, model, ""))
            if not u:
                return {
                    "requests_total": 0,
                    "success_total": 0,
                    "total_latency_ms": 0.0,
                    "last_success_at": 0.0,
                    "last_failure_at": 0.0,
                    "consecutive_failures": 0,
                }
            # 近似：成功数 = 总数 - 失败（冷却记录里的 hits）
            # 更准确的成功数需要单独记录，这里用总数估算
            cd = self._cooldowns.get(_k(tenant, provider, model, ""), {})
            failures = cd.get("hits", 0)
            success_est = max(0, u.rpd_cnt - failures)
            return {
                "requests_total": u.rpd_cnt,
                "success_total": success_est,
                "total_latency_ms": 0.0,  # 需单独记录延迟，暂留 0
                "last_success_at": 0.0,
                "last_failure_at": 0.0,
                "consecutive_failures": failures,
            }

    def record_latency(self, provider: str, model: str, latency_ms: float,
                       tenant: str = DEFAULT_TENANT):
        """记录单次请求延迟（用于 Autofree 评分）"""
        # 简单实现：累加到 usage 对象（扩展字段）
        # 实际生产建议用独立表或列，这里复用 tpm_tokens 字段的高位存延迟和
        with self._lock:
            k = _k(tenant, provider, model, "")
            u = self._usage.get(k)
            if u:
                # 存延迟累计到 tpd_tokens（复用，仅用于估算）
                u.tpd_tokens = int(u.tpd_tokens) + int(latency_ms)
                self._mark_dirty_locked()