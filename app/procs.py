# -*- coding: utf-8 -*-
"""进程层：内核进程识别、Chrome 实例枚举。

原则：**只认命令行里的绝对路径**，不认进程名。
    本机装了好几个代理软件，按名字杀 "xray.exe" 会误伤 v2rayN / Codex 管家的内核。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import psutil

from app import paths
from app.logging_setup import log

_WINDOW_CACHE: tuple[float, dict[str, list[int]]] = (0.0, {})
_WINDOW_TTL = 3.0


def norm_path(p: str | os.PathLike | None) -> str:
    """路径归一化，用于比对（大小写、斜杠、末尾反斜杠都不计较）。"""
    s = str(p or "").replace("/", "\\").rstrip("\\").lower()
    return os.path.normpath(s) if s else ""


def pids_using_config(config: Path) -> list[int]:
    """找出命令行里带这个 config 路径的 xray 进程。"""
    target = norm_path(config)
    if not target:
        return []
    found: list[int] = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            if name not in ("xray.exe", "xray", "sing-box.exe", "sing-box"):
                continue
            cmd = norm_path(" ".join(p.info["cmdline"] or []))
            if target in cmd:
                found.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def kill_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            psutil.Process(pid).kill()
            log.info("已结束内核进程 PID=%s", pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("结束 PID=%s 失败：%s", pid, e)


def chrome_instances() -> dict[str, list[int]]:
    """当前有哪些 Chrome **实例**活着，按资料目录分组：{规范化目录: [主进程 PID]}。

    面板上的「运行中」说的是**代理内核**，用户看到绿点会以为「这个 Chrome 窗口开着」。
    两件事必须分开摆出来，否则四条线全绿、实际只开了一个窗口。
    """
    global _WINDOW_CACHE
    now = time.time()
    ts, cached = _WINDOW_CACHE
    if now - ts < _WINDOW_TTL:
        return cached

    out: dict[str, list[int]] = {}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = p.info["cmdline"] or []
            if any(str(a).lower().startswith("--type=") for a in cmd):
                continue          # 渲染/GPU/工具进程不算实例
            udd = ""
            for a in cmd:
                if str(a).lower().startswith("--user-data-dir="):
                    udd = str(a).split("=", 1)[1].strip('"')
                    break
            if not udd:
                udd = str(paths.system_user_data_dir())
            out.setdefault(norm_path(udd), []).append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _WINDOW_CACHE = (now, out)
    return out


def chrome_running_with(user_data_dir: str | os.PathLike | None) -> int | None:
    """判断同一资料目录是否已有 Chrome 在跑。

    ★ 必须按目录判断：同一个 --user-data-dir 起第二个进程，Chrome 会把参数
      转交给已有实例然后自己退出，于是「点图标没反应」。
    """
    if not user_data_dir:
        return None
    target = norm_path(user_data_dir)
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if "--type=" in cmd:
                continue
            if target in norm_path(cmd):
                return p.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None
