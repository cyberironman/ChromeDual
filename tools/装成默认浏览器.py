# -*- coding: utf-8 -*-
"""把「Chrome 代理管家」注册成一个可被系统识别的浏览器，用于设为默认浏览器。

为什么需要这一步
----------------
Windows 10/11 不允许程序自己把自己设成默认浏览器：
`HKCU\\...\\UrlAssociations\\http\\UserChoice` 里有系统算出来的 Hash，
外部写入会被判为篡改并自动还原。**必须由用户在「设置 → 默认应用」里手动选一次。**

本脚本负责把「可被选中的那个条目」注册好，剩下一步交给用户点。

注册了什么（全部在 HKCU，不需要管理员）
--------------------------------------
    HKCU\\Software\\Classes\\ChromeDual.Default
        (默认)                = ChromeDual · Chrome 代理管家
        DefaultIcon           = chrome.exe,0
        shell\\open\\command   = "<pythonw>" "<sysdefault_launch.py>" "%1"
        Application           （ApplicationName 等）
    HKCU\\Software\\ChromeDual\\Capabilities
        ApplicationName / ApplicationIcon
        URLAssociations\\http  = ChromeDual.Default
        URLAssociations\\https = ChromeDual.Default
    HKCU\\Software\\RegisteredApplications
        ChromeDual = Software\\ChromeDual\\Capabilities

走哪条线路由 config\\sysdefault.json 决定，
**换线路不用重新注册**，改那个 json 即可。

用法
----
    python tools\\装成默认浏览器.py            # 注册 + 打开设置页
    python tools\\装成默认浏览器.py --check    # 只看现在是什么
    python tools\\装成默认浏览器.py --remove   # 撤销注册
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 用「当前解释器」推导 pythonw.exe —— 不写死任何用户目录
_PY = Path(sys.executable)
PYW = _PY.with_name("pythonw.exe") if _PY.with_name("pythonw.exe").is_file() else _PY
LAUNCH = ROOT / "Tray" / "sysdefault_launch.py"
CHROME = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / \
    "Google" / "Chrome" / "Application" / "chrome.exe"
CFG = ROOT / "config" / "sysdefault.json"

PROGID = "ChromeDual.Default"
APP_KEY = r"Software\ChromeDual"
CAP_KEY = APP_KEY + r"\Capabilities"
REGISTERED = r"Software\RegisteredApplications"
UA = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"


def _first_line() -> str:
    """profiles.json 里的第一条线路（sysdefault.json 没配时的兜底）。"""
    try:
        raw = json.loads((ROOT / "config" / "profiles.json").read_text(encoding="utf-8"))
        for item in raw.get("lines") or []:
            lid = str(item.get("id") or "").strip()
            if lid:
                return lid
    except Exception:  # noqa: BLE001
        pass
    return ""


def _line() -> str:
    try:
        v = str(json.loads(CFG.read_text(encoding="utf-8")).get("line") or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return _first_line()


def _display_name() -> str:
    return "ChromeDual · Chrome 代理管家"


def _set(root, path: str, name: str, value: str) -> None:
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _get(root, path: str, name: str) -> str | None:
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _del_tree(root, path: str) -> None:
    try:
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def _delete_tree(root, path: str) -> None:
    """递归删掉一个键（winreg 只能一层一层删）。"""
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
            subs, i = [], 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        for s in subs:
            _delete_tree(root, path + "\\" + s)
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def show() -> None:
    print("=== 当前系统默认浏览器 ===")
    for scheme in ("http", "https"):
        prog = _get(winreg.HKEY_CURRENT_USER, f"{UA}\\{scheme}\\UserChoice", "ProgId")
        print(f"  {scheme:<6} {prog}")
    print("\n=== 本产品的注册状态 ===")
    cmd = _get(winreg.HKEY_CURRENT_USER,
               rf"Software\Classes\{PROGID}\shell\open\command", "")
    print(f"  ProgID {PROGID}: {cmd or '<未注册>'}")
    reg = _get(winreg.HKEY_CURRENT_USER, REGISTERED, "ChromeDual")
    print(f"  RegisteredApplications: {reg or '<未注册>'}")
    print(f"  目标线路（config/sysdefault.json）: {_line()}")


def install() -> int:
    pyw = str(PYW if PYW.is_file() else Path(sys.executable))
    launch = str(LAUNCH)
    if not LAUNCH.is_file():
        print(f"✗ 找不到转接器：{LAUNCH}")
        return 2

    name = _display_name()
    cls = rf"Software\Classes\{PROGID}"

    _set(winreg.HKEY_CURRENT_USER, cls, "", name)
    _set(winreg.HKEY_CURRENT_USER, cls, "FriendlyTypeName", name)
    _set(winreg.HKEY_CURRENT_USER, rf"{cls}\DefaultIcon", "", f"{CHROME},0")
    # %1 必须带引号 —— 带空格的 URL / 带 & 的查询串不加引号会被 cmd 拆开
    _set(winreg.HKEY_CURRENT_USER, rf"{cls}\shell\open\command", "",
         f'"{pyw}" "{launch}" "%1"')
    _set(winreg.HKEY_CURRENT_USER, rf"{cls}\Application", "ApplicationName", name)
    _set(winreg.HKEY_CURRENT_USER, rf"{cls}\Application", "ApplicationIcon", f"{CHROME},0")

    _set(winreg.HKEY_CURRENT_USER, CAP_KEY, "ApplicationName", name)
    _set(winreg.HKEY_CURRENT_USER, CAP_KEY, "ApplicationDescription",
         "点开链接就用指定线路那份已登录的 Chrome，并且走它自己的代理（不动系统代理）。")
    _set(winreg.HKEY_CURRENT_USER, CAP_KEY, "ApplicationIcon", f"{CHROME},0")
    for scheme in ("http", "https"):
        _set(winreg.HKEY_CURRENT_USER, rf"{CAP_KEY}\URLAssociations", scheme, PROGID)

    _set(winreg.HKEY_CURRENT_USER, REGISTERED, "ChromeDual", CAP_KEY)

    print(f"✓ 已注册：{name}")
    print(f"   ProgID   : {PROGID}")
    print(f"   命令     : \"{pyw}\" \"{launch}\" \"%1\"")
    print(f"   线路     : {_line()}  （改 config\\sysdefault.json 可换，不用重新注册）")
    print()
    print("接下来这一步必须你手动点（Windows 强制要求，脚本代劳不了）：")
    print("   设置 → 应用 → 默认应用 → 搜索『Chrome 代理管家』→ 点它 → 设为默认")
    print("   （或者按下面自动打开的那个页面里找）")
    return 0


def open_settings() -> None:
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:defaultapps"],
                         creationflags=0x08000000)
    except Exception as e:  # noqa: BLE001
        print(f"（没能自动打开设置页：{e}，请手动打开就行）")


def remove() -> int:
    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROGID}")
    _delete_tree(winreg.HKEY_CURRENT_USER, APP_KEY)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTERED, 0, winreg.KEY_WRITE) as k:
            winreg.DeleteValue(k, "ChromeDual")
    except OSError:
        pass
    print("✓ 已撤销注册。默认浏览器若还指着它，去设置里改回 Google Chrome。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看状态")
    ap.add_argument("--remove", action="store_true", help="撤销注册")
    ap.add_argument("--no-open", action="store_true", help="不要自动打开设置页")
    a = ap.parse_args()

    if a.check:
        show()
        return 0
    if a.remove:
        return remove()

    show()
    print()
    rc = install()
    if rc == 0 and not a.no_open:
        open_settings()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
