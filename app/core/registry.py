# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""模型注册表访问器：内存 + SQLite 持久化，含已下线记录(去重)。"""
import json
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict


@dataclass
class ModelEntry:
    id: str
    provider: str
    alias_of: str = ""                    # 旧ID -> 新ID
    capability: dict = field(default_factory=dict)
    tier: str = "medium"
    status: str = "active"                # active / removed
    discovered_at: str = ""
    updated_at: str = ""
    # P0：档位与标签（免费/收费 + 能力标签），供任务感知选择器打分
    type: str = "free"                    # free | paid
    tags: list = field(default_factory=list)
    priority: int = 0                     # 越大越优先（人工置顶用）

    def to_row(self): return asdict(self)


class Registry:
    def __init__(self, cfg, db_path: Path = None):
        # 默认与 quota 同一个库（cfg.quota.db_path 已在 load_config 解析为绝对路径）
        default = getattr(getattr(cfg, "quota", None), "db_path", "data/free_router.db")
        self.db_path = Path(db_path or default)
        self.cfg = cfg
        self._models = {}                  # id -> ModelEntry (内存镜像)
        self._removed_ids = set()          # 已下线去重记录
        # P1：覆盖层墓碑（(provider, model)），静态清单/发现都不得再入池
        self.disabled = set(getattr(cfg, "disabled_models", set()) or set())
        self._init_db()
        self._load_static_models(cfg)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS models(
                id TEXT PRIMARY KEY, provider TEXT, alias_of TEXT,
                capability TEXT, tier TEXT, status TEXT,
                discovered_at TEXT, updated_at TEXT,
                type TEXT DEFAULT 'free', tags TEXT DEFAULT '[]',
                priority INTEGER DEFAULT 0)""")
            self._migrate_columns(c)
            c.execute("""CREATE TABLE IF NOT EXISTS removed_models(
                id TEXT PRIMARY KEY, removed_at TEXT)""")

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _migrate_columns(self, c):
        """老库补列：type / tags / priority（SQLite ADD COLUMN 幂等检查）。"""
        cols = [r[1] for r in c.execute("PRAGMA table_info(models)")]
        for name, ddl in (("type", "TEXT DEFAULT 'free'"),
                          ("tags", "TEXT DEFAULT '[]'"),
                          ("priority", "INTEGER DEFAULT 0")):
            if name not in cols:
                c.execute(f"ALTER TABLE models ADD COLUMN {name} {ddl}")

    def _load_static_models(self, cfg):
        """从 config.yaml 预载静态模型（作为初识种子）"""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for pname, pc in cfg.providers.items():
            for m in pc.models:
                mid = m.get("id")
                if not mid:
                    continue
                if (pname, mid) in self.disabled or mid in self._removed_ids:
                    continue
                entry = ModelEntry(
                    id=mid, provider=pname,
                    capability=m.get("capability", {}) or {},
                    tier=m.get("tier", pc.tier_quality),
                    discovered_at=now, updated_at=now,
                    type=str(m.get("type", getattr(pc, "type", "free") or "free")),
                    tags=list(m.get("tags", getattr(pc, "tags", None) or []) or []),
                    priority=int(m.get("priority", 0) or 0),
                )
                self._models[entry.id] = entry
                self._upsert(entry)

    def _upsert(self, e: ModelEntry):
        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO models
                (id, provider, alias_of, capability, tier, status,
                 discovered_at, updated_at, type, tags, priority)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (e.id, e.provider, e.alias_of,
                       json.dumps(e.capability), e.tier, e.status,
                       e.discovered_at, e.updated_at,
                       getattr(e, "type", "free"), json.dumps(getattr(e, "tags", []) or []),
                       int(getattr(e, "priority", 0) or 0)))

    def list_active(self) -> list:
        return [m for m in self._models.values() if m.status == "active"]

    def get(self, mid): return self._models.get(mid)

    def add(self, mid, provider, capability=None, tier="medium",
            type=None, tags=None, priority=0):
        """新增模型（自动发现 / 动态增删用）"""
        if (provider, mid) in getattr(self, "disabled", set()):
            return False
        if mid in self._removed_ids:
            return False
        if mid in self._models and self._models[mid].status == "active":
            return False
        pc = self.cfg.providers.get(provider) if getattr(self, "cfg", None) else None
        if type is None:
            type = getattr(pc, "type", "free") or "free"
        if tags is None:
            tags = list(getattr(pc, "tags", None) or [])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        e = ModelEntry(id=mid, provider=provider,
                       capability=capability or {}, tier=tier,
                       discovered_at=now, updated_at=now,
                       type=type, tags=list(tags or []), priority=int(priority or 0))
        self._models[mid] = e
        self._upsert(e)
        return True

    def remove(self, mid):
        """下线模型：移出候选池 + 去重记录"""
        e = self._models.get(mid)
        if e and e.status == "active":
            e.status = "removed"
            self._upsert(e)
        # 去重记录
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._removed_ids.add(mid)
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO removed_models VALUES(?,?)", (mid, now))

    def rename(self, old_id, new_id):
        """别名变更：旧ID 映射到新ID"""
        e = self._models.get(old_id)
        if not e:
            return
        e.alias_of = new_id
        # 将新ID 并入池（若新ID 不存在则继承旧能力）
        if new_id not in self._models:
            self.add(new_id, e.provider, e.capability, e.tier)
        self._upsert(e)

    def unremove(self, mid):
        """显式重新加入：清除墓碑（自动发现仍不会自行复活被删模型）。"""
        self._removed_ids.discard(mid)
        try:
            with self._conn() as c:
                c.execute("DELETE FROM removed_models WHERE id=?", (mid,))
        except Exception:
            pass

    def get_removed_ids(self) -> set:
        return set(self._removed_ids)

    def stats(self) -> dict:
        from collections import Counter
        active = self.list_active()
        return {
            "total": len(self._models),
            "active": len(active),
            "removed": len(self._removed_ids),
            "by_provider": self._count_by_provider(),
            "by_type": dict(Counter(getattr(m, "type", "free") for m in active)),
            "disabled": len(getattr(self, "disabled", set()) or set()),
        }

    def _count_by_provider(self) -> dict:
        from collections import Counter
        return dict(Counter(m.provider for m in self._models.values()))


# 便捷：能力过滤工具
def has_vision(cap: dict) -> bool:
    return bool(cap.get("vision"))


def cap_score(cap: dict, task: str = None) -> float:
    """能力综合分（可按任务加权）"""
    base = float(cap.get("score", 50))
    if not task:
        return base
    # 代码任务偏 code，推理偏 reason
    w = {"code": 1.5, "reason": 1.3}.get(task, 1.0)
    dim = cap.get(task, base)
    return 0.5 * base + 0.5 * dim * w / 1.5