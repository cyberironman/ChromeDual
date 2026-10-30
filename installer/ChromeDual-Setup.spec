# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 生成单文件 `ChromeDual-Setup.exe`。

它把 dist\\ChromeDual 整个目录**塞进自己体内**，运行时解到临时目录，
再由 installer\\setup_main.py 复制到用户选的位置。

为什么不直接把 dist 打成 zip 让用户解压：
    小白拿到 zip 会解压到「下载」文件夹，然后双击里面的 exe 直接跑 ——
    程序在一个随时会被清掉的临时目录里安家，登录态跟着没了。
    安装器就是为了把这件事钉死在固定位置，并且留下卸载入口。
"""
from pathlib import Path
import os

ROOT = Path(SPECPATH).resolve().parent
# build.py 会在环境变量里指定主程序打到了哪个目录（输出目录可能带时间戳后缀，
# 因为某些环境不允许删除已存在的 dist）。找不到就用默认的 dist\ChromeDual。
DIST = Path(os.environ.get("CHROMEDUAL_DIST") or (ROOT / "dist" / "ChromeDual"))

if not (DIST / "ChromeDual.exe").is_file():
    raise SystemExit(
        "\n[打包中止] 找不到 dist\\ChromeDual\\ChromeDual.exe\n"
        "先打主程序：python -m PyInstaller --noconfirm installer\\ChromeDual.spec\n"
        "或者直接跑：python installer\\build.py（会自动按顺序来）\n"
    )

a = Analysis(
    [str(ROOT / "installer" / "setup_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(DIST), "payload/ChromeDual")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "_tkinter", "pystray", "PIL", "webview", "psutil",
        "clr_loader", "pythonnet", "pytest", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

# onefile：a.binaries / a.datas 全部塞进 EXE，没有 COLLECT
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ChromeDual-Setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                      # 安装进度要看得见
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "guard.ico"),
    version=str(ROOT / "installer" / "version_info_setup.txt"),
)
