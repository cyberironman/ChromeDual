# -*- coding: utf-8 -*-
"""ChromeDual · Chrome 代理管家 —— 程序入口。

一条线路 = 一个独立内核 + 一份完全独立的 Chrome 资料目录。
点哪条线路，才起哪条线路的内核；其余线路一律不动。

不带参数：启动托盘管家。
带参数（桌面快捷方式用）：
    --open profile-a     直接开这条线路的 Chrome 窗口
    --target example     按重点网址自动挑一条能打开的线路再开窗
    -h / --help          看帮助
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _print_help() -> None:
    print(__doc__)


def main() -> None:
    args = [a for a in sys.argv[1:] if a.strip()]
    if args and args[0] in ("-h", "--help", "/?"):
        _print_help()
        return
    if args and (args[0].startswith("--open") or args[0].startswith("--target")):
        from app.cli import main as cli_main
        raise SystemExit(cli_main(args))

    from app.tray import main as tray_main
    tray_main()


if __name__ == "__main__":
    main()
