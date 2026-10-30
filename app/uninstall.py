# -*- coding: utf-8 -*-
"""卸载 —— `ChromeDual.exe --uninstall`。

为什么卸载要程序自己做
----------------------
「设置 → 应用 → 卸载」点了以后，Windows 只会执行注册表里那条 UninstallString。
如果那条命令是个 bat，用户会看到一个黑窗口一闪、什么都不确定；
如果直接删目录，程序自己正在运行，exe 删不掉。

所以：程序自己起来 → 征求确认 → 停掉自己拉起的进程 → 交给一个**独立于安装
目录之外**的 PowerShell 延迟删除。它不是安装目录里的文件，所以能把整个目录
连同自己一起删干净。

数据怎么处理
------------
卸载时问一句：「资料要不要一起删？」

  * 一起删：登录态、Chrome 资料、备份、日志全清 —— 换机器前想干净点选这个
  * 保留  ：只删程序文件，`data\\` 和 `config\\` 留着 —— 重装后登录还在
"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

from app import paths

APP_NAME = "ChromeDual"
APP_TITLE = "Chrome 代理管家"
UNINST_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"

_MB_YESNOCANCEL = 0x3
_MB_ICONWARNING = 0x30
_MB_ICONINFO = 0x40
_IDYES, _IDNO, _IDCANCEL = 6, 7, 2

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008


def _box(text: str, title: str = "ChromeDual", flags: int = 0) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:  # noqa: BLE001
        print(text)
        return _IDCANCEL


def _kill_own_children() -> None:
    """把安装目录里起来的进程全停掉（内核 xray.exe + 另一个管家实例）。

    ★ 刻意**不**实例化 Guard：卸载时把整个程序初始化一遍（读配置、起保活
      线程、抢端口）纯属节外生枝，而且它可能反过来把 xray 又拉起来。
      直接按「进程 exe 在安装目录下」来判断，简单且不会误伤别的程序。
    """
    try:
        import ctypes.wintypes as wt

        k = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        arr = (wt.DWORD * 2048)()
        need = wt.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need)):
            return
        count = need.value // ctypes.sizeof(wt.DWORD)
        mine = os.getpid()
        root = str(paths.ROOT).lower()
        for i in range(count):
            pid = arr[i]
            if not pid or pid == mine:
                continue
            h = k.OpenProcess(0x0400 | 0x0001 | 0x0010, False, pid)
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    p = buf.value.lower()
                    if p.startswith(root) and (p.endswith("xray.exe") or p.endswith("chromedual.exe")):
                        k.TerminateProcess(h, 0)
            finally:
                k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass


def _remove_shortcuts() -> None:
    """删桌面 / 开始菜单 / 启动项里的快捷方式。"""
    targets: list[Path] = []

    desktop = None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as k:
            v, _ = winreg.QueryValueEx(k, "Desktop")
            desktop = Path(os.path.expandvars(v))
    except OSError:
        desktop = Path.home() / "Desktop"
    targets.append(desktop / f"{APP_TITLE}.lnk")
    targets.append(Path.home() / "Desktop" / f"{APP_TITLE}.lnk")   # OneDrive 桌面兜底

    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    menu = Path(appdata) / r"Microsoft\Windows\Start Menu\Programs"
    targets.append(menu / f"{APP_TITLE}.lnk")
    targets.append(menu / "Startup" / f"{APP_TITLE}.lnk")

    for lnk in targets:
        try:
            lnk.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_registry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except OSError:
        pass


def _schedule_delete(root: Path, keep_data: bool) -> bool:
    """交给安装目录外的 PowerShell 延迟删：自己删不掉自己。"""
    lines = [
        "Start-Sleep -Milliseconds 1800",
    ]
    if keep_data:
        # 只删程序，留 config / data / var
        keep = ", ".join(f"'{x}'" for x in ("config", "data", "var", "logs"))
        lines.append(f"$root = '{root}'")
        lines.append("Get-ChildItem -LiteralPath $root -Force | "
                     f"Where-Object {{ $_.Name -notin @({keep}) }} | "
                     "ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force "
                     "-ErrorAction SilentlyContinue }")
        lines.append("if ((Get-ChildItem -LiteralPath $root -Force | Measure-Object).Count -eq 0) { "
                     "Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }")
    else:
        lines.append(f"Remove-Item -LiteralPath '{root}' -Recurse -Force -ErrorAction SilentlyContinue")

    script = "; ".join(lines)
    try:
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            creationflags=CREATE_NO_WINDOW | DETACHED,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(Path(os.environ.get("TEMP", "."))),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def run(silent: bool = False) -> int:
    root = Path(paths.ROOT)

    if not silent:
        ans = _box(
            f"要卸载「{APP_TITLE}」吗？\n\n"
            f"程序目录：{root}\n\n"
            "点「是」：连 Chrome 资料、登录状态、日志一起删（彻底清干净）\n"
            "点「否」：只删程序，保留 data\\ 和 config\\（重装后登录还在）\n"
            "点「取消」：什么也不做",
            "卸载 ChromeDual", _MB_YESNOCANCEL | _MB_ICONWARNING,
        )
        if ans == _IDCANCEL:
            return 1
        keep_data = (ans == _IDNO)
    else:
        keep_data = False

    print("正在停止进程…", flush=True)
    _kill_own_children()
    time.sleep(0.8)

    print("正在清理快捷方式与注册表…", flush=True)
    _remove_shortcuts()
    _remove_registry()

    print("正在删除程序文件…", flush=True)
    if not _schedule_delete(root, keep_data):
        print("自动删除失败，请手动删除目录：" + str(root), flush=True)
        if not silent:
            _box(f"没能自动删除，请手动删掉这个文件夹：\n\n{root}", "卸载 ChromeDual",
                 _MB_ICONWARNING)
        return 2

    time.sleep(2.2)
    if not silent:
        _box(
            "卸载完成。" + ("\n\n资料目录已保留：" + str(root) if keep_data else ""),
            "卸载 ChromeDual", _MB_ICONINFO,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    silent = any(a.lower() in ("/s", "/silent", "-s", "--silent") for a in argv)
    return run(silent=silent)


if __name__ == "__main__":
    raise SystemExit(main())
