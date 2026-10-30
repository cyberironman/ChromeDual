# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 生成 dist\\ChromeDual\\ChromeDual.exe。

为什么用 onedir 而不是 onefile
------------------------------
onefile 每次双击都要把几十 MB（Python 运行时 + pythonnet + WebView2 的
托管程序集）解包到临时目录，冷启动 5-10 秒，而且杀毒软件对「自解压可执行
文件」特别敏感，容易误报。onedir 只有一份，启动 1 秒内，改内核也不用重打包。

用户不需要关心 dist 里有一堆文件 —— installer\\setup_main.py 会把它打成
一个「安装 ChromeDual.exe」，装完桌面只有一个图标。

资源怎么进来
------------
`sys._MEIPASS` 指向 `dist/ChromeDual/_internal`，代码里 paths.RES 就是它，
所以这里把 assets / bin / app\\panel.html / config 模板 / tools 都放进包内，
运行期按 RES 相对路径去找，和源码形态完全一致。
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

#: 只读资源：(源路径, 包内目录)
DATAS = [
    (ROOT / "assets", "assets"),
    (ROOT / "bin", "bin"),
    (ROOT / "app" / "panel.html", "app"),
    (ROOT / "config" / "profiles.example.json", "config"),
    (ROOT / "config" / "targets.example.json", "config"),
    (ROOT / "config" / "sysdefault.example.json", "config"),
    (ROOT / "tools", "tools"),
    (ROOT / "LICENSE", "."),
    (ROOT / "README.md", "."),
]
DATAS = [(str(s), d) for s, d in DATAS if Path(s).exists()]

#: 这些是 PyInstaller 静态分析抓不到的（运行期才 import，或者按平台分支加载）
HIDDEN = [
    "app.deps",
    "app.selfrun",
    "app.bootstrap",
    "app.login_guard",
    "app.login_backup",
    "pystray._win32",                 # pystray 按平台挑后端
    "webview.platforms.edgechromium",  # pywebview 的 WebView2 后端
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
]

#: 明确排除：本机没有 tkinter，其余是 GUI/科学计算大件，带着只是白占 100MB
EXCLUDES = [
    "tkinter", "_tkinter", "turtle", "turtledemo",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "matplotlib", "numpy", "pandas", "scipy", "IPython",
    "notebook", "jupyter", "pytest", "setuptools._distutils.tests",
    "test", "unittest", "pydoc_data",
]

a = Analysis(
    [str(ROOT / "ChromeDual.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ChromeDual",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                     # 托盘程序，不要黑窗口
    disable_windowed_traceback=False,  # 崩溃时给个对话框，比静默消失强
    icon=str(ROOT / "assets" / "guard.ico"),
    version=str(ROOT / "installer" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ChromeDual",
)
