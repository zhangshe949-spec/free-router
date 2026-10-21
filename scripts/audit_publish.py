# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""发布前硬审计：确认「远端仓库」与「本地 git 历史」里没有你的真实 API key。

与 scripts/scan_secrets.py 的区别：
- scan_secrets.py 靠**形态**判断（sk-* / rc-* / gho_* ...），可能漏；
- 本脚本拿**你本机 .env 里的真实 key 值**逐字节比对，零漏报。

用法：
    python scripts/audit_publish.py                 # 审计本地工作区 + git 历史
    python scripts/audit_publish.py --remote        # 额外审计 GitHub 远端（需 GH_TOKEN）
    python scripts/audit_publish.py --remote --repo zhangshe949-spec/free-router

退出码：0 = 干净；1 = 发现泄露（必须先处理，禁止继续推送）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 可能存放真实密钥的位置（本机）
KEY_SOURCES = [
    Path.home() / ".free-router" / ".env",
    ROOT / "config" / ".env",
    ROOT / ".env",
    Path.home() / "AppData" / "Local" / "hermes" / "skills" / "free-router" / "config" / ".env",
]

# 不该出现在仓库里的文件（即便内容为空也不该提交）
FORBIDDEN_NAMES = {".env", "providers.local.yaml", "credentials.json", "secrets.yaml"}

# 形态扫描（与 scan_secrets.py 一致，作为第二道防线）
PATTERNS = [
    ("github-token", re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("github-pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("openai-style-key", re.compile(rb"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("amd-radeon-key", re.compile(rb"\brc-[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("huggingface-token", re.compile(rb"\bhf_[A-Za-z0-9]{30,}")),
]


def load_real_keys() -> dict[str, str]:
    """从本机 .env 收集真实 key 的值（只留在内存，绝不打印）。"""
    keys: dict[str, str] = {}
    for src in KEY_SOURCES:
        if not src.exists():
            continue
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip("'\"")
            if v and len(v) >= 8:
                keys.setdefault(k.strip(), v)
    return keys


def scan_bytes(data: bytes, keys: dict[str, str], where: str) -> list[str]:
    hits: list[str] = []
    for name, value in keys.items():
        if value.encode() in data:
            hits.append(f"{where}: 命中真实密钥变量 {name}")
    for label, rx in PATTERNS:
        for m in rx.finditer(data):
            hits.append(f"{where}: 命中形态 {label} (len={len(m.group(0))})")
    return hits


def audit_worktree(keys: dict[str, str]) -> list[str]:
    """只审计「真正会被发布」的文件。

    判定依据用 git 自己的视图：
    - git ls-files --cached --others --exclude-standard  → 已跟踪 + 未跟踪且未被忽略
    被 .gitignore 排除的运行时文件（config/.env、data/*.db 等）不会上传，
    因此不算泄露；但若它们出现在已跟踪列表里，则说明规则失效 → 立即报警。
    """
    hits: list[str] = []
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    publishable = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True).stdout.split()

    # 被忽略却已被跟踪 = 忽略规则失效（真泄露路径）
    tracked_set = set(tracked)
    for name in FORBIDDEN_NAMES:
        for p in tracked:
            if Path(p).name == name:
                hits.append(f"{p}: 文件名在禁止清单中（.gitignore 规则已失效），禁止上传")

    for rel in publishable:
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            hits.extend(scan_bytes(path.read_bytes(), keys, rel))
        except OSError:
            pass
    return hits


def audit_git_history(keys: dict[str, str]) -> list[str]:
    hits: list[str] = []
    out = subprocess.run(["git", "rev-list", "--objects", "--all"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    for line in out.strip().splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, path = parts
        t = subprocess.run(["git", "cat-file", "-t", sha], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
        if t != "blob":
            continue
        blob = subprocess.run(["git", "cat-file", "blob", sha], cwd=ROOT,
                              capture_output=True).stdout
        hits.extend(scan_bytes(blob, keys, f"git-history:{path}"))
    return hits


def audit_remote(keys: dict[str, str], repo: str) -> list[str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return ["--remote 需要 GH_TOKEN 环境变量（未提供，已跳过远端审计）"]
    proxy = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:10808"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))

    def api(path: str):
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json"})
        with opener.open(req, timeout=60) as r:
            return json.loads(r.read())

    hits: list[str] = []
    tree = api("/git/trees/main?recursive=1")
    for entry in tree.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry["path"]
        req = urllib.request.Request(
            f"https://raw.githubusercontent.com/{repo}/main/{path}", headers={"User-Agent": "audit"})
        with opener.open(req, timeout=60) as r:
            data = r.read()
        if path.split("/")[-1] in FORBIDDEN_NAMES:
            hits.append(f"remote:{path}: 禁止上传的文件名")
        hits.extend(scan_bytes(data, keys, f"remote:{path}"))
    return hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true", help="同时审计 GitHub 远端")
    ap.add_argument("--repo", default="zhangshe949-spec/free-router")
    args = ap.parse_args(argv[1:])

    keys = load_real_keys()
    print(f"[audit] 载入本机真实密钥变量 {len(keys)} 个（值不打印）")
    if not keys:
        print("[audit] 警告：未找到任何本机 .env，审计只能靠形态扫描")

    hits: list[str] = []
    hits += audit_worktree(keys)
    print("[audit] 工作区扫描完成")
    hits += audit_git_history(keys)
    print("[audit] git 历史扫描完成")
    if args.remote:
        hits += audit_remote(keys, args.repo)
        print("[audit] GitHub 远端扫描完成")

    if hits:
        print(f"\n[audit] FAIL：发现 {len(hits)} 处疑似泄露，禁止推送：")
        for h in sorted(set(hits)):
            print("  -", h)
        print("\n处理步骤：1) 立刻到对应服务商后台吊销该 key；2) 用 git filter-repo 清历史；"
              "3) 强制推送后重跑本脚本。")
        return 1
    print("\n[audit] PASS：工作区 / git 历史 / 远端均未发现真实密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
