"""模型更新通知（inbox）。

自动发现在「新增/下线」模型时写入事件，供管理面板集中提示：
用户据此判断是否保留新出现的免费模型（面板上可一键「保留」或「移除」）。

持久化：data/notifications.json（与 DB 同目录，重启不丢）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock


class Notifier:
    def __init__(self, path: Path, max_events: int = 100):
        self.path = Path(path)
        self.max = int(max_events)
        self._lock = Lock()
        self.events: list = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> list:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return list(d.get("events") or [])
                if isinstance(d, list):
                    return d
        except Exception:
            pass
        return []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"events": self.events[-self.max:]},
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass

    # ---------- 写入 ----------
    def add(self, kind: str, provider: str, model: str, **extra) -> dict:
        """记录一条事件；同一 (kind, provider, model) 未读时只累加次数，不刷屏。"""
        with self._lock:
            for e in self.events:
                if (e.get("kind") == kind and e.get("provider") == provider
                        and e.get("model") == model and not e.get("ack")):
                    e["ts"] = time.time()
                    e["count"] = int(e.get("count", 1) or 1) + 1
                    for k, v in extra.items():
                        if v not in (None, "", [], {}):
                            e[k] = v
                    self._save()
                    return e
            ev = {
                "id": f"{int(time.time() * 1000)}-{provider}-{str(model)[:60]}",
                "kind": kind, "provider": provider, "model": model,
                "ts": time.time(), "ack": False, "count": 1,
            }
            for k, v in extra.items():
                if v not in (None, "", [], {}):
                    ev[k] = v
            self.events.append(ev)
            self.events = self.events[-self.max:]
            self._save()
            return ev

    # ---------- 读取 / 确认 ----------
    def unread_count(self) -> int:
        return sum(1 for e in self.events if not e.get("ack"))

    def list(self, only_unread: bool = False, limit: int = 100) -> list:
        evs = [e for e in self.events if (not only_unread or not e.get("ack"))]
        evs.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return evs[:limit]

    def ack(self, ids=None, ack_all: bool = False) -> int:
        with self._lock:
            n = 0
            for e in self.events:
                if e.get("ack"):
                    continue
                if ack_all or (ids and e.get("id") in ids):
                    e["ack"] = True
                    n += 1
            if n:
                self._save()
            return n

    def clear(self) -> int:
        with self._lock:
            n = len(self.events)
            self.events = []
            self._save()
            return n
