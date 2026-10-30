# -*- coding: utf-8 -*-
"""ChromeDual 安装程序 —— 双击一个 exe，装好、建图标、能卸载。

它自己不依赖任何第三方库
------------------------
安装器在「干净电脑」上必须 100% 能跑起来，所以刻意只用标准库：
  * 不用 tkinter（很多精简版 Python 没有，本项目就是）
  * 不用 pywebview（它依赖 WebView2 —— 而这正是安装器要修的东西之一）
界面走 Windows 控制台，顺便能显示真实进度条。

装 / 升级 / 卸载的边界
----------------------
安装目录长这样：

    <安装目录>\\
        ChromeDual.exe          <- 程序（升级时整体覆盖）
        _internal\\              <- 依赖与资源（升级时整体覆盖）
        config\\  data\\  var\\    <- 用户的东西（**升级时一个字节都不碰**）

这是整件事最重要的规矩：用户重装/升级，登录态、Chrome 资料、备份全都在。
所以复制时对 `config` / `data` / `var` 这三个顶层目录一律「已存在就跳过」。

用法
----
    ChromeDual-Setup.exe               交互式安装
    ChromeDual-Setup.exe /S            静默安装（装到默认位置并启动）
    ChromeDual-Setup.exe /S /D=D:\\Apps\\ChromeDual    静默 + 指定位置
"""
from __future__ import annotations

import base64
import ctypes
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

APP_NAME = "ChromeDual"
APP_TITLE = "Chrome 代理管家"
APP_VER = "1.0.1"
APP_ID = "ChromeDual"
UNINST_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
PUBLISHER = "ChromeDual (MIT License)"
URL = "https://github.com/cyberironman/ChromeDual"

#: 装到这些顶层目录里的东西是**用户的**，升级时跳过
USER_DIRS = {"config", "data", "var", "logs"}

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010


# ================================================================ 控制台美化

def _enable_ansi() -> None:
    """打开控制台的 ANSI 转义支持（Win10 1511+）。不支持就自动降级为无色。"""
    if os.name != "nt":
        return
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return
        k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:  # noqa: BLE001
        pass


class C:
    on = False
    R = "\033[0m"
    B = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    GRAY = "\033[90m"


def c(text: str, color: str) -> str:
    return f"{color}{text}{C.R}" if C.on else text


def say(text: str = "", color: str = "") -> None:
    print(c(text, color) if color else text, flush=True)


def banner() -> None:
    say()
    say("  " + "=" * 58, C.CYAN)
    say(f"   {APP_TITLE}  ·  ChromeDual  {APP_VER}", C.B + C.CYAN)
    say("   多身份 Chrome 多开 + 按需代理   安装程序", C.GRAY)
    say("  " + "=" * 58, C.CYAN)
    say()


def step(n: int, total: int, text: str) -> None:
    say(f"  [{n}/{total}] {text}", C.B)


def ok(text: str = "完成") -> None:
    say(f"          [OK] {text}", C.GREEN)


def warn(text: str) -> None:
    say(f"          [!] {text}", C.YELLOW)


def fail(text: str) -> None:
    say(f"          [X] {text}", C.RED)


def progress(done: int, total: int, width: int = 28) -> None:
    if not total:
        return
    filled = int(width * done / total)
    bar = "#" * filled + "-" * (width - filled)
    pct = done * 100 // total
    sys.stdout.write(f"\r          [{bar}] {pct:3d}%")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


# ================================================================ 环境

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def payload_dir() -> Path:
    """安装器里内嵌的程序文件在哪。

    打包后 = `_MEIPASS/payload/ChromeDual`；源码直接跑时 = 项目根的 dist 目录。
    """
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return root / "payload" / "ChromeDual"
    here = Path(__file__).resolve().parent
    return here.parent / "dist" / "ChromeDual"


def running_instances(target: Path) -> list[int]:
    """找出「正在从目标目录运行的 ChromeDual」进程，安装前必须让它们退出。"""
    if os.name != "nt":
        return []
    try:
        import ctypes.wintypes as wt
        k = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        arr = (wt.DWORD * 2048)()
        need = wt.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need)):
            return []
        count = need.value // ctypes.sizeof(wt.DWORD)
        PROC_QUERY = 0x0400 | 0x0010
        found = []
        needle = str(target).lower()
        for i in range(count):
            pid = arr[i]
            if not pid:
                continue
            h = k.OpenProcess(PROC_QUERY, False, pid)
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    if buf.value.lower().startswith(needle) and buf.value.lower().endswith(".exe"):
                        found.append(pid)
            finally:
                k.CloseHandle(h)
        return found
    except Exception:  # noqa: BLE001
        return []


# ================================================================ 快捷方式

def make_shortcut(lnk: Path, target: Path, arguments: str = "", workdir: Path | None = None,
                  icon: Path | None = None, desc: str = "") -> bool:
    """用 PowerShell 的 WScript.Shell 建 .lnk。

    为什么不用纯 Python：pylnk3 写出来的 lnk 没有 LinkInfo 段，
    资源管理器「打开文件所在位置」会跑到 System32 去（踩过）。
    WScript.Shell 是系统自带的正规实现，而且 PowerShell 一定有。

    中文路径 / 引号 / 空格一律用 -EncodedCommand（base64 UTF-16LE）传，
    彻底绕开 cmd 的编码与引号转义问题。
    """
    # PowerShell 单引号字符串里，单引号本身要用两个单引号转义。
    # 路径里出现 O'Brien 这种目录名时不转义会把整条命令弄坏。
    def q(v) -> str:
        return str(v).replace("'", "''")

    ps = (
        "$ErrorActionPreference='Stop';"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
        f"$s.TargetPath='{q(target)}';"
        f"$s.Arguments='{q(arguments)}';"
        f"$s.WorkingDirectory='{q(workdir or target.parent)}';"
        f"$s.IconLocation='{q(icon or target)},0';"
        f"$s.Description='{q(desc)}';"
        "$s.Save();"
        f"if (!(Test-Path '{q(lnk)}')) {{ exit 3 }}"
    )
    try:
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode == 0 and lnk.exists()
    except Exception:  # noqa: BLE001
        return False


def desktop_dir() -> Path:
    """真正的桌面在哪。OneDrive 接管桌面时会在 OneDrive 下，不能死写 ~/Desktop。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as k:
            v, _ = winreg.QueryValueEx(k, "Desktop")
            return Path(os.path.expandvars(v))
    except OSError:
        return Path.home() / "Desktop"


def start_menu_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / r"Microsoft\Windows\Start Menu\Programs"


# ================================================================ 复制文件

def copy_tree(src: Path, dst: Path, keep_user_dirs: bool = True) -> tuple[int, int]:
    """把程序文件复制过去，返回 (复制文件数, 跳过文件数)。

    ★ 顶层 `config`/`data`/`var`/`logs` 如果**安装前**就已存在，整棵跳过 —— 那是用户资料。

    注意 keep 集合必须在开始复制前一次算好：如果边复制边判断
    `(dst / name).exists()`，第一轮刚把空的 config 目录建出来，
    紧接着它下面的文件就被当成「用户已有的」全部跳过了 —— 首次安装也会中招。
    """
    copied = skipped = 0

    keep: set[str] = set()
    if keep_user_dirs and dst.exists():
        for name in USER_DIRS:
            if (dst / name).exists():
                keep.add(name)

    entries: list[Path] = list(src.rglob("*"))
    total = max(1, len(entries))

    dst.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(entries, 1):
        rel = p.relative_to(src)
        if rel.parts and rel.parts[0].lower() in keep:
            if p.is_file():
                skipped += 1
            continue
        out = dst / rel
        if p.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
            copied += 1
        if i % 25 == 0 or i == total:
            progress(i, total)
    return copied, skipped


# ================================================================ 注册表

def register_uninstall(target: Path) -> bool:
    """登记到「设置 → 应用 → 已安装的应用」，用户才有正规的卸载入口。"""
    try:
        exe = target / f"{APP_NAME}.exe"
        size_kb = 0
        for p in target.rglob("*"):
            if p.is_file():
                size_kb += p.stat().st_size // 1024
        ico = target / "_internal" / "assets" / "guard.ico"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
            for name, val in (
                ("DisplayName", f"{APP_TITLE}（ChromeDual）"),
                ("DisplayVersion", APP_VER),
                ("Publisher", PUBLISHER),
                ("DisplayIcon", str(ico if ico.is_file() else exe)),
                ("InstallLocation", str(target)),
                ("UninstallString", f'"{exe}" --uninstall'),
                ("QuietUninstallString", f'"{exe}" --uninstall /S'),
                ("HelpLink", URL),
                ("URLInfoAbout", URL),
            ):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, max(1, size_kb))
        return True
    except OSError:
        return False


def set_autostart(target: Path, enable: bool) -> bool:
    """开机自启：在「启动」文件夹放/删一个 lnk。

    用启动文件夹而不是注册表 Run 键 —— 用户能在资源管理器里亲眼看到它、
    自己删掉，不会被「怎么开机就自己冒出来」吓到。
    """
    lnk = start_menu_dir() / "Startup" / f"{APP_TITLE}.lnk"
    try:
        if not enable:
            lnk.unlink(missing_ok=True)
            return True
        exe = target / f"{APP_NAME}.exe"
        ico = target / "_internal" / "assets" / "guard.ico"
        return make_shortcut(lnk, exe, "", target, ico if ico.is_file() else None,
                             "Chrome 代理管家（托盘）")
    except OSError:
        return False


# ================================================================ 运行环境

def ensure_webview2(no_install: bool = False) -> None:
    """WebView2 是控制面板的渲染引擎，缺了面板打不开。装之前问一句。

    `no_install=True` 时只报告状态、不弹窗、不下载（演练模式 / 自动化测试用）。
    """
    if os.name != "nt":
        return
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    found = None
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}"),
    ):
        try:
            with winreg.OpenKey(root, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and str(pv).strip() not in ("", "0.0.0.0"):
                    found = str(pv)
                    break
        except OSError:
            continue

    if found:
        ok(f"WebView2 {found}")
        return

    warn("没检测到 WebView2 运行时（控制面板窗口需要它）")
    if no_install:
        warn("演练模式：不自动安装，正式安装时会问你")
        return
    ans = ctypes.windll.user32.MessageBoxW(
        None,
        "这台电脑没有 WebView2 运行时，Chrome 代理管家的控制面板会打不开。\n\n"
        "现在从微软官网下载并安装吗？（约 2 MB，需要联网）\n\n"
        "选「否」也能装完，托盘菜单照常可用。",
        "ChromeDual 安装程序", 0x4 | 0x30,
    )
    if ans != 6:
        warn("已跳过，稍后可在程序里用 --check 复查")
        return

    url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
    exe = Path(os.environ.get("TEMP", ".")) / "MicrosoftEdgeWebView2Setup.exe"
    say("          下载 WebView2 安装包 …")
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "ChromeDual-Setup"})
        with urllib.request.urlopen(req, timeout=60) as r, open(exe, "wb") as fh:  # noqa: S310
            shutil.copyfileobj(r, fh)
        say("          静默安装中（1-2 分钟）…")
        subprocess.call([str(exe), "/silent", "/install"], creationflags=CREATE_NO_WINDOW)
        say("          WebView2 安装程序已执行完毕")
    except Exception as e:  # noqa: BLE001
        fail(f"自动安装失败：{e}")
        warn("可以稍后手动装：https://developer.microsoft.com/microsoft-edge/webview2/")


# ================================================================ 主流程

def ask_dir(default: Path) -> Path:
    """问装到哪。直接回车 = 默认位置。"""
    say(f"  安装位置  {c(str(default), C.CYAN)}", C.B)
    say("  （直接回车用这个位置；也可以把别的文件夹拖进这个窗口）", C.GRAY)
    say()
    try:
        raw = input("  > ").strip().strip('"').strip("'")
    except (EOFError, KeyboardInterrupt):
        say()
        raise SystemExit(1) from None
    if not raw:
        return default
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    # 拖进来的如果是 exe 本身，取它所在的目录
    if p.suffix.lower() == ".exe":
        p = p.parent
    return p


def run_setup(silent: bool = False, force_dir: Path | None = None) -> int:
    _enable_ansi()
    C.on = sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"
    # 演练模式：只复制文件，不建快捷方式、不写注册表、不启动。
    # 给「在自己机器上验证安装包」和 CI 用 —— 装个测试包不该往桌面扔图标。
    dry = os.environ.get("CHROMEDUAL_SETUP_DRYRUN") == "1"

    banner()
    if dry:
        say("  [演练模式] 只复制文件，不改桌面 / 注册表 / 不会启动", C.YELLOW)
        say()

    src = payload_dir()
    if not src.is_dir() or not (src / f"{APP_NAME}.exe").is_file():
        fail(f"安装包里没有程序文件：{src}")
        say("  这通常说明安装包没打包完整，请重新下载。", C.GRAY)
        if not silent:
            input("\n  按回车退出…")
        return 2

    target = force_dir or (default_dir() if silent else ask_dir(default_dir()))
    say()
    say(f"  目标目录：{c(str(target), C.B + C.CYAN)}")
    say()

    total_steps = 6
    n = 0

    # 1) 环境检查 ------------------------------------------------------
    n += 1
    step(n, total_steps, "检查运行环境")
    ensure_webview2(no_install=dry)

    # 2) 关掉正在运行的实例 --------------------------------------------
    n += 1
    step(n, total_steps, "检查是否有正在运行的程序")
    pids = running_instances(target)
    if pids:
        warn(f"有 {len(pids)} 个实例正在运行，先请它们退出 …")
        k32 = ctypes.windll.kernel32
        for pid in pids:
            h = k32.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
            if h:
                k32.TerminateProcess(h, 0)
                k32.CloseHandle(h)
        time.sleep(1.2)
    ok("没有占用" if not pids else "已清理")

    # 3) 复制文件 ------------------------------------------------------
    n += 1
    step(n, total_steps, "复制程序文件")
    try:
        copied, skipped = copy_tree(src, target)
    except PermissionError as e:
        fail(f"没有写入权限：{e}")
        say("  请换一个位置（比如放到 D 盘自己建的文件夹），或右键「以管理员身份运行」。",
            C.GRAY)
        if not silent:
            input("\n  按回车退出…")
        return 3
    ok(f"复制 {copied} 个文件" + (f"，保留用户数据 {skipped} 个" if skipped else ""))

    # 4) 快捷方式 ------------------------------------------------------
    n += 1
    step(n, total_steps, "创建快捷方式")
    exe = target / f"{APP_NAME}.exe"
    ico = target / "_internal" / "assets" / "guard.ico"
    ico_arg = ico if ico.is_file() else None
    made = []
    if dry:
        warn("演练模式，跳过")
    else:
        d_lnk = desktop_dir() / f"{APP_TITLE}.lnk"
        if make_shortcut(d_lnk, exe, "", target, ico_arg, "Chrome 代理管家"):
            made.append("桌面")
        s_lnk = start_menu_dir() / f"{APP_TITLE}.lnk"
        if make_shortcut(s_lnk, exe, "", target, ico_arg, "Chrome 代理管家"):
            made.append("开始菜单")
        if made:
            ok("、".join(made))
        else:
            fail("快捷方式没建成（不影响使用，可直接运行 exe）")

    # 5) 卸载信息 ------------------------------------------------------
    n += 1
    step(n, total_steps, "登记卸载信息")
    if dry:
        warn("演练模式，跳过")
    else:
        ok("已加入「应用和功能」" if register_uninstall(target) else "写入注册表失败（跳过）")

    if not silent and not dry:
        ans = ctypes.windll.user32.MessageBoxW(
            None,
            "要让它开机自动在托盘里等着吗？\n\n"
            "（选「是」以后每次开机自动运行；不需要就点「否」，"
            "以后在托盘右键菜单里随时能开。）",
            "ChromeDual 安装程序", 0x4 | 0x40,
        )
    else:
        ans = 7
    if ans == 6:
        ok("已加入开机启动" if set_autostart(target, True) else "开机启动没设上（跳过）")

    # 6) 启动 ----------------------------------------------------------
    n += 1
    step(n, total_steps, "启动程序")
    say()
    if dry:
        warn("演练模式，跳过启动")
    elif not silent:
        try:
            subprocess.Popen([str(exe)], cwd=str(target), creationflags=CREATE_NO_WINDOW)
            ok("已启动，看右下角托盘")
        except OSError as e:
            fail(f"启动失败：{e}")
    else:
        ok("静默安装完成")

    say()
    say("  " + "=" * 58, C.GREEN)
    say(f"   装好了。程序位置：{target}", C.GREEN)
    say(f"   桌面图标「{APP_TITLE}」，右下角托盘里也能找到。", C.GREEN)
    say(f"   要卸载：设置 → 应用 → 已安装的应用 → 找「{APP_TITLE}」。", C.GRAY)
    say("  " + "=" * 58, C.GREEN)
    say()
    if not silent:
        input("  按回车关闭这个窗口…")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    silent = any(a.lower() in ("/s", "/silent", "-s", "--silent") for a in argv)

    force: Path | None = None
    for a in argv:
        if a.lower().startswith("/d="):
            force = Path(a[3:].strip('"'))
        elif a.lower().startswith("--dir="):
            force = Path(a[6:].strip('"'))

    try:
        return run_setup(silent=silent, force_dir=force)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        say()
        fail(f"安装过程中出错：{type(e).__name__}: {e}")
        try:
            ctypes.windll.user32.MessageBoxW(
                None, f"安装失败：\n{e}\n\n请截图反馈到 {URL}/issues",
                "ChromeDual 安装程序", 0x10)
        except Exception:  # noqa: BLE001
            pass
        if not silent:
            input("\n  按回车退出…")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
