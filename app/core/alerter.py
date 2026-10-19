"""错误告警（P6）：错误率超阈值时通过 webhook / SMTP 通知，可选临时启用收费兜底。

默认关闭（alerter.enabled=false），不引入任何依赖（urllib + smtplib 标准库）。

配置示例：
    alerter:
      enabled: false
      type: webhook            # webhook | smtp | none
      webhook_url: ""
      smtp_host: "", smtp_port: 587, smtp_user: "", smtp_password: "",
      smtp_from: "", smtp_to: []
      error_rate_threshold: 0.3
      window_seconds: 300
      min_interval_seconds: 600   # 两次告警最小间隔，避免风暴
      fallback_to_paid: false     # 触发时临时把收费模型提前（需池中有 type: paid）
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
from collections import deque
from email.message import EmailMessage
from typing import Deque, Optional, Tuple
from urllib import request as _req

logger = logging.getLogger("free_router.alerter")


class Alerter:
    def __init__(self, cfg):
        a = getattr(cfg, "alerter", None) or {}
        self.enabled = bool(a.get("enabled", False))
        self.type = str(a.get("type", "none"))
        self.webhook_url = a.get("webhook_url", "")
        self.smtp_host = a.get("smtp_host", "")
        self.smtp_port = int(a.get("smtp_port", 587))
        self.smtp_user = a.get("smtp_user", "")
        self.smtp_password = a.get("smtp_password", "")
        self.smtp_from = a.get("smtp_from", "")
        self.smtp_to = list(a.get("smtp_to") or [])
        self.threshold = float(a.get("error_rate_threshold", 0.3))
        self.window = float(a.get("window_seconds", 300))
        self.min_interval = float(a.get("min_interval_seconds", 600))
        self.fallback_to_paid = bool(a.get("fallback_to_paid", False))
        self._events: Deque[Tuple[float, bool]] = deque(maxlen=500)
        self._last_alert = 0.0
        self.last_alert_at: Optional[float] = None

    # ---------- 事件记录 ----------
    def record(self, ok: bool) -> None:
        now = time.time()
        self._events.append((now, bool(ok)))
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def error_rate(self) -> Tuple[int, int, float]:
        total = len(self._events)
        if total == 0:
            return 0, 0, 0.0
        errs = sum(1 for _, ok in self._events if not ok)
        return total, errs, errs / total

    # ---------- 判定与发送 ----------
    def maybe_alert(self, detail: str = "") -> Optional[str]:
        if not self.enabled:
            return None
        total, errs, rate = self.error_rate()
        if total < 5 or rate < self.threshold:
            return None
        if time.time() - self._last_alert < self.min_interval:
            return None
        subject = f"[free-router] 错误率 {rate:.0%}（{errs}/{total}，近 {int(self.window)}s）"
        body = f"{subject}\n\n{detail}\n\n提示：自动顺延仍在工作；若持续失败请检查各提供商额度/密钥。"
        self._last_alert = time.time()
        self.last_alert_at = self._last_alert
        self.send(subject, body)
        return subject

    def send(self, subject: str, body: str) -> bool:
        try:
            if self.type == "webhook" and self.webhook_url:
                data = json.dumps({"subject": subject, "body": body}).encode("utf-8")
                req = _req.Request(self.webhook_url, data=data,
                                   headers={"Content-Type": "application/json"})
                _req.urlopen(req, timeout=8).read()
                return True
            if self.type == "smtp" and self.smtp_host and self.smtp_to:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = self.smtp_from or self.smtp_user
                msg["To"] = ", ".join(self.smtp_to)
                msg.set_content(body)
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as s:
                    s.starttls()
                    if self.smtp_user:
                        s.login(self.smtp_user, self.smtp_password)
                    s.send_message(msg)
                return True
        except Exception as e:                    # 告警失败不得影响主流程
            logger.warning("告警发送失败: %s", e)
        return False

    def snapshot(self) -> dict:
        total, errs, rate = self.error_rate()
        return {"enabled": self.enabled, "type": self.type,
                "window_samples": total, "errors": errs, "error_rate": round(rate, 4),
                "threshold": self.threshold, "last_alert_at": self.last_alert_at}