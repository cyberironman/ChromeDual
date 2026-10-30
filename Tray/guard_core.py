# -*- coding: utf-8 -*-
"""⚠ 兼容外壳 —— 核心逻辑已拆分到 D:\\ChromeDual\\app\\ 下的多个模块：

    app/paths.py          路径（唯一来源）
    app/net.py            端口 / 站点探测
    app/procs.py          进程与 Chrome 实例识别
    app/pac.py            PAC 静态服务
    app/config_store.py   配置读写
    app/line.py           单条线路
    app/guard.py          总控

保留本文件只为两件事：
    1. `pythonw Tray\\guard_core.py --pac-only`（sysdefault_launch 的兜底 PAC 服务）
    2. 万一还有老脚本 `import guard_core`，名字不会断。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import paths  # noqa: E402
from app.config_store import load_profiles, read_startup_urls, save_profiles  # noqa: E402
from app.guard import Guard, serve_pac_only  # noqa: E402
from app.line import Line  # noqa: E402
from app.logging_setup import log, setup_logging  # noqa: E402
from app.net import (free_port, port_listeners, port_open, probe_proxy,  # noqa: E402
                     probe_target)
from app.pac import PacServer  # noqa: E402
from app.procs import (chrome_instances, chrome_running_with,  # noqa: E402
                       kill_pids, norm_path, pids_using_config)

# 老代码里用到的模块级常量
BASE = paths.APP
LOG_DIR = paths.LOGS
PROFILES = paths.PROFILES
TARGETS = paths.TARGETS
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

__all__ = [
    "BASE", "LOG_DIR", "PROFILES", "TARGETS", "CREATE_NO_WINDOW", "Guard", "Line",
    "PacServer", "chrome_instances", "chrome_running_with", "default_user_data_dir",
    "free_port", "kill_pids", "load_profiles", "log", "norm_path", "pids_using_config",
    "port_listeners", "port_open", "probe_proxy", "probe_target", "read_startup_urls",
    "save_profiles", "serve_pac_only", "setup_logging",
]


def default_user_data_dir() -> str:
    """系统默认资料目录（仅用于迁移/对比；运行期线路不许挂它）。"""
    return str(paths.system_user_data_dir())


if __name__ == "__main__":
    if "--pac-only" in sys.argv:
        raise SystemExit(serve_pac_only())
    print("guard_core 现在是兼容外壳；核心在 app/ 下，托盘入口是 Chrome代理管家.py")
