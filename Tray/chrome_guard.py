# -*- coding: utf-8 -*-
"""⚠ 兼容入口 —— 真正的代码已经搬到 D:\\ChromeDual\\app\\ 下面。

这个文件保留，是因为还有几处老入口指向它：
    * 桌面快捷方式「Chrome代理管家.lnk」→ pythonw.exe "…\\Tray\\chrome_guard.py"
    * 注册表默认浏览器转接器 ChromeSplit → Tray\\sysdefault_launch.py（它会拉起这个）
    * 开机自启项
新入口：D:\\ChromeDual\\Chrome代理管家.py（等价）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tray import main  # noqa: E402

if __name__ == "__main__":
    main()
