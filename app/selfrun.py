# -*- coding: utf-8 -*-
"""「再拉起一个我自己」—— 全项目唯一允许拼这条命令行的地方。

为什么不能各写各的
------------------
源码形态和打包形态的拉起方式**完全不同**：

    源码：pythonw.exe  "…\\ChromeDual.py"  --open profile-a
    打包：ChromeDual.exe                   --open profile-a

以前 cli.py 和 tray.py 各自写了一份
`Path(sys.executable).with_name("pythonw.exe")`，源码形态没问题，
一旦打成 exe 就会变成「拿 ChromeDual.exe 当解释器、去执行一个根本不存在的
ChromeDual.py」—— 双击没反应，日志里也看不出所以然。

所以统一收在这里：调用方只说「我要带这些参数再起一个」，别的不用管。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app import paths

CREATE_NO_WINDOW = 0x08000000
#: 源码形态要挂的控制台程序（不是独立窗口那种）
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def command(args: list[str] | None = None) -> list[str]:
    """拼出「再拉起自己」的完整命令行。"""
    args = list(args or [])
    if paths.FROZEN:
        return [str(paths.ENTRY)] + args

    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    interpreter = pyw if pyw.is_file() else exe
    return [str(interpreter), str(paths.ENTRY)] + args


def spawn(args: list[str] | None = None, *, hidden: bool = True,
          **popen_kw) -> subprocess.Popen:
    """拉起来，立刻返回（不等它退）。默认不弹黑窗口、不继承我们的管道。"""
    kw = dict(
        cwd=str(paths.ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if hidden:
        kw["creationflags"] = CREATE_NO_WINDOW
    kw.update(popen_kw)
    return subprocess.Popen(command(args), **kw)


def shortcut_target(icon: bool = True) -> tuple[str, str, str]:
    """给「建快捷方式」用的三元组：(目标 exe, 参数串, 工作目录)。

    快捷方式的 TargetPath 必须是 **exe**，脚本路径只能放 Arguments；
    反过来（TargetPath 写 .py）资源管理器会拿记事本去开它。
    """
    if paths.FROZEN:
        return str(paths.ENTRY), "", str(paths.ROOT)

    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    interpreter = pyw if pyw.is_file() else exe
    return str(interpreter), f'"{paths.ENTRY}"', str(paths.ROOT)


def pythonw() -> str:
    """能给 .py 用的解释器路径（源码形态）/ exe 自己（打包形态）。

    装成默认浏览器、开机自启这类要写进注册表或 lnk 的地方都用它。
    """
    if paths.FROZEN:
        return str(paths.ENTRY)
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.is_file() else exe)
