# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""仓库密钥扫描：发现疑似硬编码凭据即以非零码退出。

用途：
  1. CI 在每次 push / PR 上运行，防止密钥（含本项目各提供商 key）误提交；
  2. 本地可手动执行：python scripts/scan_secrets.py [路径]

规则：
  - 只扫描纳入版本控制的文本文件，跳过 .git/、data/、.venv/ 等；
  - 允许清单见 ALLOW_MARKERS（示例/占位文件整文件跳过）；
  - 命中即打印 file:line 与脱敏后的片段（绝不打印完整密钥）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 整文件跳过：模板/占位文件，天然不含真值
SKIP_FILES = {
    ".env.example",
    "LICENSE",
    "NOTICE",
    ".gitignore",
}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "data", "node_modules", "dist", "build"}
SKIP_SUFFIX = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".db", ".db-wal", ".db-journal", ".zip", ".exe", ".dll", ".so", ".pdf", ".woff", ".woff2"}

# 明确的凭据形态
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("amd-radeon-key", re.compile(r"\brc-[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("bearer-literal", re.compile(r"[Bb]earer\s+[A-Za-z0-9_\-\.=]{24,}")),
    # key/secret/token/password 被赋了一个像真值的非空串
    ("assigned-secret", re.compile(
        r"(?i)\b(?:api[_\-]?key|apikey|secret[_\-]?key|access[_\-]?key|client[_\-]?secret|"
        r"auth[_\-]?token|private[_\-]?key|password|passwd)\b\s*[:=]\s*"
        r"[\"']?(?![Ee]mpty)(?![Nn]one)(?![Nn]ull)[\"']?[A-Za-z0-9_\-\/\+=]{16,}[\"']?"
    )),
]


def mask(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "***" + value[-2:]


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIX:
            continue
        yield path


def scan(root: Path) -> list[tuple[Path, int, str, str]]:
    hits: list[tuple[Path, int, str, str]] = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:2048]:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 2000:
                line = line[:2000]
            for label, rx in PATTERNS:
                for m in rx.finditer(line):
                    hits.append((path, lineno, label, mask(m.group(0))))
    return hits


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    if not root.is_dir():
        print(f"[scan-secrets] 不是目录: {root}")
        return 2
    hits = scan(root)
    if not hits:
        print(f"[scan-secrets] OK: {root} 未发现疑似硬编码凭据")
        return 0
    print(f"[scan-secrets] FAIL: 发现 {len(hits)} 处疑似凭据")
    for path, lineno, label, snippet in hits:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        print(f"  {rel}:{lineno}  [{label}]  {snippet}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
