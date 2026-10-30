# -*- coding: utf-8 -*-
"""ChromeDual · Chrome 代理管家 —— 程序入口。

一条线路 = 一个独立内核 + 一份完全独立的 Chrome 资料目录。
点哪条线路，才起哪条线路的内核；其余线路一律不动。

不带参数：启动托盘管家（会先自检依赖，缺什么提示你点一下自动装）。
带参数（桌面快捷方式用）：
    --open profile-a     直接开这条线路的 Chrome 窗口
    --target example     按重点网址自动挑一条能打开的线路再开窗
    --check              只做依赖体检、不启动（排障用）
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

    if args and args[0] in ("--uninstall", "/uninstall"):
        # 卸载放在最前面：要卸的时候不该先逼用户把缺的库装齐
        from app import uninstall

        raise SystemExit(uninstall.main(args[1:]))

    if args and args[0] == "--check":
        from app import deps

        raise SystemExit(deps.main(["--check"]))

    # 开某条线路的窗口：只需要标准库，不碰 GUI 依赖，所以直接走、不做拦截
    if args and (args[0].startswith("--open") or args[0].startswith("--target")):
        from app.cli import main as cli_main

        raise SystemExit(cli_main(args))

    # ---------------- 托盘形态：这里是唯一需要 pystray / pywebview 的地方 ----
    from app import deps, paths

    paths.ensure_dirs()

    # ★ 顺序很讲究：自检必须在 import pystray / webview **之前**。
    #   否则缺库时抛的是一屏 ImportError 栈，新手完全看不懂；现在会弹一个
    #   「缺 pystray，点『是』自动装」的窗口，点完就装好、装完自动继续启动。
    if not deps.ensure(interactive=True):
        raise SystemExit(1)

    from app.tray import main as tray_main

    tray_main()


if __name__ == "__main__":
    main()
