# -*- coding: utf-8 -*-
"""Free-Router 看门狗：常驻守护，服务掉线自动拉起（Windows，无窗口）。

设计要点：
- 与 autostart.vbs 不同，本进程常驻，每 interval 秒探活一次，掉线即重启；
- 自身用 pythonw 运行（无控制台）→ sys.stdout/stderr 可能为 None，先重定向日志；
- 单实例：用 data/watchdog.pid 记录自身 PID，重复启动直接退出；
- 拉起子进程用 DETACHED_PROCESS，父进程退出不影响服务。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # .../scripts
BASE = ROOT.parent                                # skill 根
HEALTH = "http://127.0.0.1:8787/health"
LOG = BASE / "data" / "watchdog.log"
PIDFILE = BASE / "data" / "watchdog.pid"
INTERVAL = 60


def _log(msg: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8", errors="replace") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def _already_running() -> bool:
    """单实例检查：PID 文件里的进程是否还活着。"""
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    if pid == os.getpid():
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in (out.stdout or "")
    except Exception:
        return False


def _healthy(timeout: float = 5.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _spawn_server():
    """用 pythonw（无窗口）拉起服务；自包含 run_server.py 会自行 chdir/syspath。"""
    pyw = Path(sys.executable)
    if pyw.name.lower() != "pythonw.exe":
        cand = pyw.with_name("pythonw.exe")
        if cand.exists():
            pyw = cand
    srv = ROOT / "run_server.py"
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        subprocess.Popen([str(pyw), str(srv)], cwd=str(BASE),
                         creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _log(f"服务掉线 → 已重新拉起 ({pyw.name})")
    except Exception as e:
        _log(f"拉起失败: {type(e).__name__}: {e}")


def main():
    if _already_running():
        _log("已有看门狗在运行，本实例退出")
        return
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    _log(f"看门狗启动 pid={os.getpid()} 间隔={INTERVAL}s")
    fails = 0
    while True:
        if not _healthy():
            fails += 1
            # 连续 2 次失败才拉起，避免探活抖动导致双实例
            if fails >= 2:
                if not _healthy(8.0):      # 再给一次机会（服务可能正在启动）
                    _spawn_server()
                    time.sleep(20)         # 给它启动时间
                fails = 0
        else:
            fails = 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    # 无控制台（pythonw）时 stdout/stderr 为 None，任何 print/logging 都会崩
    if sys.stdout is None or sys.stderr is None:
        _log_devnull = open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = _log_devnull
        if sys.stderr is None:
            sys.stderr = _log_devnull
    main()
