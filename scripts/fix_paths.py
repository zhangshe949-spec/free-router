# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""把仓库内写死的旧安装路径改为当前机器的真实路径。

仓库曾安装在 L:\\free-router（外置盘），该盘已不在。现在仓库位于
C:\\Users\\qin\\free-router，脚本与文档里的绝对路径需要同步，否则
开机自启（注册表 Run 项指向 .vbs 绝对路径）会静默失效。

用法：
    python scripts/fix_paths.py [目标根目录]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

OLD_ROOTS = ["L:/free-router", "L:\\free-router", "/l/free-router"]
TEXT_SUFFIX = {".md", ".py", ".yaml", ".yml", ".vbs", ".toml", ".txt", ".ps1", ".sh", ".json"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "data", "node_modules"}


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    new_root = root.as_posix()
    changed: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIX:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        original = text
        for old in OLD_ROOTS:
            text = text.replace(old, new_root)
        # 归一化重复斜杠
        text = re.sub(re.escape(new_root) + r"/{2,}", new_root + "/", text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed.append(str(path.relative_to(root)))
    print(f"[fix-paths] 根目录 -> {new_root}")
    if changed:
        print("[fix-paths] 已更新:")
        for c in changed:
            print("  ", c)
    else:
        print("[fix-paths] 无需改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
