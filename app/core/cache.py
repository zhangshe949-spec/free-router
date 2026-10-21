# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""响应缓存 + 离线容灾（P2）。纯标准库 SQLite 实现，零新增依赖。

两种用法：
- serve_hits=True ：命中直接返回缓存（省额度）；
- 默认 serve_hits=False：只在「全部免费模型不可用」时回退到最近一次成功响应，
  保证任务不中断（响应头标记 X-Free-Router-Cache: STALE-FALLBACK）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class ResponseCache:
    def __init__(self, db_path: Path, enabled: bool = True,
                 ttl_seconds: int = 86400, max_entries: int = 2000,
                 serve_hits: bool = False):
        self.db_path = Path(db_path)
        self.enabled = bool(enabled)
        self.ttl = int(ttl_seconds)
        self.max_entries = int(max_entries)
        self.serve_hits = bool(serve_hits)
        self._init_db()

    def _init_db(self):
        if not self.enabled:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS response_cache(
                key TEXT PRIMARY KEY, tenant TEXT, provider TEXT, model TEXT,
                ts REAL, payload TEXT)""")

    # ---------- key ----------
    @staticmethod
    def make_key(body: dict, tenant: str = "default") -> str:
        """只把影响语义的字段入哈希（忽略 stream 等传输细节与会变的 model 字段）。"""
        norm = {
            "messages": body.get("messages"),
            "temperature": body.get("temperature"),
            "max_tokens": body.get("max_tokens"),
            "tools": body.get("tools"),
            "tenant": tenant,
        }
        data = json.dumps(norm, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    # ---------- 读写 ----------
    def get(self, body: dict, tenant: str = "default",
            allow_stale: bool = False) -> Optional[dict]:
        if not self.enabled:
            return None
        key = self.make_key(body, tenant)
        with sqlite3.connect(self.db_path) as c:
            row = c.execute("SELECT ts, payload FROM response_cache WHERE key=?",
                            (key,)).fetchone()
        if not row:
            return None
        ts, payload = row
        fresh = (time.time() - float(ts)) <= self.ttl
        if not fresh and not allow_stale:
            return None
        try:
            data = json.loads(payload)
        except Exception:
            return None
        if isinstance(data, dict):
            data.setdefault("_free_router", {})
            data["_free_router"]["cache"] = "HIT" if fresh else "STALE-FALLBACK"
            data["_free_router"]["cached_at"] = float(ts)
        return data

    def set(self, body: dict, response: Any, tenant: str = "default",
            provider: str = "", model: str = "") -> None:
        if not self.enabled or not isinstance(response, dict):
            return
        if "choices" not in response:                 # 只缓存成功响应
            return
        key = self.make_key(body, tenant)
        payload = json.dumps(response, ensure_ascii=False, default=str)
        try:
            with sqlite3.connect(self.db_path) as c:
                c.execute("""INSERT OR REPLACE INTO response_cache
                             VALUES(?,?,?,?,?,?)""",
                          (key, tenant, provider, model, time.time(), payload))
                c.execute("""DELETE FROM response_cache WHERE key IN (
                    SELECT key FROM response_cache ORDER BY ts DESC
                    LIMIT -1 OFFSET ?)""", (self.max_entries,))
        except Exception:
            pass

    def purge_expired(self) -> int:
        if not self.enabled:
            return 0
        cutoff = time.time() - self.ttl
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute("DELETE FROM response_cache WHERE ts < ?", (cutoff,))
            return cur.rowcount or 0

    def stats(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        with sqlite3.connect(self.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0]
        return {"enabled": True, "entries": int(n), "ttl_seconds": self.ttl,
                "serve_hits": self.serve_hits}