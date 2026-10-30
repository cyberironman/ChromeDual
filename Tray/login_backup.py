# -*- coding: utf-8 -*-
"""⚠ 兼容外壳 —— 登录态备份已移到 app/login_backup.py。

★ 接口变了（这是刻意的）：现在**每个函数都必须传线路 id**，
  备份根也从全局的 _cookie_backup 改成了 var\\backups\\<线路>\\。
  老的 `snapshot(udd, profile, tag)` 写法已废弃 —— 它分不清线路，
  会出现「A 线路的 Cookie 灌进 B 线路」。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.login_backup import (ITEMS, KEEP, chrome_busy, cookie_count,  # noqa: E402,F401
                             fingerprint, list_backups, restore, snapshot)

__all__ = ["ITEMS", "KEEP", "chrome_busy", "cookie_count", "fingerprint",
           "list_backups", "restore", "snapshot"]
