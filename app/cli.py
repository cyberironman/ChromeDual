# -*- coding: utf-8 -*-
"""命令行入口：给桌面快捷方式用的「直接开某条线路的窗口」。

    pythonw ChromeDual.py --open profile-a    开 profile-a 线路的窗口
    pythonw ChromeDual.py --open profile-b    开 profile-b 线路
    pythonw ChromeDual.py --target example    按重点网址自动挑线路开

设计：先问控制口在不在（在就直接发指令，1 秒内开窗）；
不在就把管家拉起来，等它就绪再发 —— 这样桌面图标点一下总能出窗口，
不需要用户先记得「管家开着没」。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

from app import paths, ports

CREATE_NO_WINDOW = 0x08000000


def control_port() -> int:
    """控制口端口是随机分配的，每次现读 var/run/ports.json（不再写死 10895）。"""
    return ports.get("ctl", 0)


def send(cmd: str, timeout: float = 3.0) -> str | None:
    port = control_port()
    if not port:
        return None
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout)
        s.sendall(cmd.encode())
        s.settimeout(timeout)
        r = s.recv(64).decode("utf-8", "ignore").strip()
        s.close()
        return r or ""
    except OSError:
        return None


def ensure_running(wait: float = 25.0) -> bool:
    if send("ping", 1.0) is not None:
        return True
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    try:
        subprocess.Popen([str(exe), str(paths.ENTRY)], cwd=str(paths.ROOT),
                         creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if send("ping", 1.0) is not None:
            return True
        time.sleep(0.5)
    return False


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    kind, _, value = argv[0].partition("=")
    value = value or (argv[1] if len(argv) > 1 else "")
    value = value.strip()
    if not value:
        return 2

    if not ensure_running():
        return 3
    cmd = f"open:{value}" if kind == "--open" else f"opentarget:{value}"
    reply = send(cmd, 5.0)
    return 0 if reply == "ok" else 4
