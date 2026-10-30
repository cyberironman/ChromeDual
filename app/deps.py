# -*- coding: utf-8 -*-
"""依赖自检与一键安装 —— 「缺什么，点一下装什么」。

为什么需要这个模块
------------------
本软件在两种形态下运行，缺的东西完全不一样：

  ① **源码形态**（你有 Python，双击 ChromeDual.py）
     缺的是 pip 包：pystray / pywebview / psutil / pillow。
     不装就跑不起来，而且报错是英文 ImportError，新手完全看不懂。

  ② **打包形态**（ChromeDual.exe，自带 Python 和全部 pip 包）
     pip 包不缺，缺的是**系统级运行时**：
       * WebView2 运行时 —— 控制面板窗口靠它渲染，缺了面板打不开
         （Win10 1803 以前、LTSC 精简版、某些 Ghost 系统会没有）
       * .NET Framework 4.6.2+ —— pythonnet 的宿主，Win10/11 自带 4.8
     Windows 上不能 pip 装这些，只能下载官方安装包静默安装。

所以这里做统一的 `ensure()`：先查、把缺的列给用户看、用户点「自动安装」
才开始下载安装，装完自动复检。**绝不静默联网装东西**。

界面为什么不是 tkinter
----------------------
本机这套 Python 是精简发行版，**没有 tkinter**；打包进去还会多 10MB。
所以只用 Win32 原生 MessageBox（ctypes 调 user32）+ 需要时开一个控制台
窗口显示下载进度。零第三方依赖，任何 Windows 都能用，打包也不会失败。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 清单定义

#: 源码形态需要、打包形态已经内置的 pip 包。 (import 名, pip 安装名)
PY_PACKAGES: tuple[tuple[str, str], ...] = (
    ("psutil", "psutil>=5.9"),
    ("pystray", "pystray>=0.19.5"),
    ("PIL", "pillow>=10.0"),
    ("webview", "pywebview>=4.4"),
)

#: WebView2 运行时的官方 GUID（微软固定值，所有版本都一样）
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
#: 微软官方 Evergreen Bootstrapper 永久短链（会自动挑 x86/x64）
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
#: .NET Framework 4.8 离线安装包（Win7/8.1 才需要，Win10+ 自带）
NETFX_URL = "https://go.microsoft.com/fwlink/?LinkId=2085155"
#: Xray 内核官方发布包（代理内核本体，36MB 的 xray.exe 就在里面）
XRAY_URL = "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-windows-64.zip"

#: .NET Framework 4.6.2 的 Release 号阈值
_NETFX_MIN_RELEASE = 394802

_CREATE_NO_WINDOW = 0x08000000
_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONERROR = 0x10
_MB_ICONWARNING = 0x30
_MB_ICONINFO = 0x40
_IDYES = 6


# ---------------------------------------------------------------- 基础工具

def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


def _msgbox(text: str, title: str = "ChromeDual", flags: int = _MB_OK) -> int:
    """Win32 原生消息框。返回点击的按钮 ID（是 = 6）。"""
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags))
    except Exception:  # noqa: BLE001
        # 极端情况（无桌面会话）退回控制台
        print(f"[{title}] {text}", file=sys.stderr)
        return _IDYES


def _ask(text: str, title: str = "ChromeDual") -> bool:
    return _msgbox(text, title, _MB_YESNO | _MB_ICONWARNING) == _IDYES


def _alloc_console(title: str) -> bool:
    """给 windowed exe 临时挂一个控制台，用来显示下载 / 安装进度。

    已经有控制台（源码形态在 cmd 里跑）就直接复用，返回 False 表示「借来的」。
    """
    try:
        if ctypes.windll.kernel32.GetConsoleWindow():
            return False
        if not ctypes.windll.kernel32.AllocConsole():
            return False
        ctypes.windll.kernel32.SetConsoleTitleW(title)
        # 重绑 sys.stdout/stderr，否则 print 还指向原来的空设备
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        return True
    except Exception:  # noqa: BLE001
        return False


def _free_console() -> None:
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 检查：pip 包

def missing_packages() -> list[tuple[str, str]]:
    """返回缺失的 pip 包清单。打包形态永远返回空（都内置了）。"""
    if is_frozen():
        return []
    import importlib.util

    out = []
    for mod, spec in PY_PACKAGES:
        try:
            if importlib.util.find_spec(mod) is None:
                out.append((mod, spec))
        except (ImportError, ValueError, ModuleNotFoundError):
            out.append((mod, spec))
    return out


# ---------------------------------------------------------------- 检查：系统运行时

def webview2_version() -> str | None:
    """查 WebView2 运行时版本；没装返回 None。

    三处都要查：机器级 64 位、机器级 32 位（WOW6432Node）、当前用户级
    （Evergreen 按用户装时只写 HKCU）。
    """
    if os.name != "nt":
        return None
    import winreg

    keys = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and str(pv).strip() not in ("", "0.0.0.0"):
                    return str(pv).strip()
        except OSError:
            continue
    return None


def netfx_release() -> int:
    """查 .NET Framework 4.x 的 Release 号；查不到返回 0。"""
    if os.name != "nt":
        return 0
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
        ) as k:
            val, _ = winreg.QueryValueEx(k, "Release")
            return int(val)
    except (OSError, ValueError):
        return 0


def missing_runtimes() -> list[dict]:
    """返回缺失的系统运行时清单，每项含 id / 名称 / 说明 / 下载地址。"""
    if os.name != "nt":
        return []

    out: list[dict] = []

    if webview2_version() is None:
        out.append({
            "id": "webview2",
            "name": "WebView2 运行时",
            "why": "控制面板窗口要用它渲染。缺了只能看托盘菜单，面板打不开。",
            "url": WEBVIEW2_URL,
            "size": "约 2 MB 在线安装",
            "silent": ["/silent", "/install"],
            "file": "MicrosoftEdgeWebView2Setup.exe",
        })

    rel = netfx_release()
    if rel and rel < _NETFX_MIN_RELEASE:
        out.append({
            "id": "netfx",
            "name": ".NET Framework 4.6.2 以上",
            "why": "当前只有 4.6.2 以下版本，pywebview 的 Windows 后端跑不起来。",
            "url": NETFX_URL,
            "size": "约 70 MB",
            "silent": ["/q", "/norestart"],
            "file": "ndp48-x86-x64-allos-enu.exe",
        })

    return out


def report() -> dict:
    """一次性汇总当前环境的依赖状况（给启动自检和面板显示用）。"""
    return {
        "frozen": is_frozen(),
        "python": sys.version.split()[0],
        "core": paths_core(),
        "packages_missing": [spec for _mod, spec in missing_packages()],
        "runtimes_missing": [r["name"] for r in missing_runtimes()],
        "webview2": webview2_version() or "",
        "netfx_release": netfx_release(),
    }


def paths_core() -> str:
    """内核就位了没。返回实际路径，没有则空串。"""
    try:
        from app import paths

        return str(paths.XRAY) if paths.ensure_core() else ""
    except Exception:  # noqa: BLE001
        return ""


def core_missing() -> bool:
    return not paths_core()


def install_core(cache_dir: Path, log=_log) -> bool:
    """下载 Xray 内核并解出 xray.exe。

    仓库里不放 bin\\*.exe（36MB 的二进制塞进 git 会让 clone 变得很痛苦），
    所以从 GitHub 下载源码来用的人第一次启动会缺内核，这里负责补齐。
    """
    import zipfile

    from app import paths

    log("")
    log("正在获取 Xray 内核（约 12 MB）")
    log("-" * 58)

    pkg = cache_dir / "Xray-windows-64.zip"
    if not pkg.is_file():
        if not _download(XRAY_URL, pkg, log, "Xray 内核"):
            log("下载失败。也可以自己下载后放到：" + str(paths.BIN_DATA))
            log("  " + XRAY_URL)
            return False

    try:
        with zipfile.ZipFile(pkg) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("xray.exe")), None)
            if not name:
                log("压缩包里没找到 xray.exe，可能官方改了打包结构。")
                return False
            paths.BIN_DATA.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(paths.XRAY, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        try:
            os.chmod(paths.XRAY, 0o755)
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001
        log(f"解压失败：{type(e).__name__}: {e}")
        return False

    ok = paths.XRAY.is_file()
    log("内核就位：" + str(paths.XRAY) if ok else "内核仍未就位")
    return ok


def healthy() -> bool:
    return not core_missing() and not missing_packages() and not missing_runtimes()


# ---------------------------------------------------------------- 安装：pip 包

def pip_install(specs: list[str], log=_log) -> bool:
    """用当前解释器的 pip 装包。没有写系统目录的权限时自动加 --user。"""
    if not specs:
        return True

    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    attempts = [base + specs, base + ["--user"] + specs]

    log("")
    log("正在安装：" + "、".join(specs))
    log("-" * 58)
    for cmd in attempts:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW if is_frozen() else 0,
            )
        except OSError as e:
            log(f"启动 pip 失败：{e}")
            return False

        assert proc.stdout is not None
        for line in proc.stdout:
            log("  " + line.rstrip())
        code = proc.wait()
        if code == 0:
            log("-" * 58)
            log("安装完成。")
            return True

        log(f"pip 返回 {code}，换 --user 模式重试…")

    log("安装失败。请手动执行：")
    log(f'  "{sys.executable}" -m pip install ' + " ".join(specs))
    return False


# ---------------------------------------------------------------- 安装：系统运行时

def _download(url: str, dest: Path, log=_log, note: str = "") -> bool:
    """带百分号进度的下载。用 urllib 而不是 requests —— 少一个依赖。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"下载 {note or dest.name} …")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ChromeDual/installer"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last_pct = -5
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = got * 100 // total
                        if pct >= last_pct + 5:
                            last_pct = pct
                            log(f"  {pct:3d}%  {got / 1048576:.1f} / {total / 1048576:.1f} MB")
                    else:
                        log(f"  {got / 1048576:.1f} MB")
        tmp.replace(dest)
        log(f"下载完成：{dest.name}（{dest.stat().st_size / 1048576:.1f} MB）")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"下载失败：{type(e).__name__}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        # 下载失败时把官方地址告诉用户，让他自己点
        log(f"请手动下载：{url}")
        return False


def install_runtime(item: dict, cache_dir: Path, log=_log) -> bool:
    """下载并静默安装一个系统运行时。返回是否成功（装完会复检）。"""
    log("")
    log(f"正在安装 {item['name']}（{item['size']}）")
    log("-" * 58)

    target = cache_dir / item["file"]
    if not target.is_file():
        if not _download(item["url"], target, log, item["name"]):
            return False
    else:
        log(f"复用已下载的安装包：{target}")

    cmd = [str(target)] + list(item.get("silent") or [])
    log("静默安装中（可能要 1-2 分钟，请不要关窗口）…")
    try:
        code = subprocess.call(cmd, creationflags=_CREATE_NO_WINDOW)
    except OSError as e:
        log(f"无法运行安装包：{e}")
        return False

    if code not in (0, 1638, 3010):     # 1638=已装更高版本, 3010=需重启
        log(f"安装包返回 {code}")

    # 复检：安装包返回码不可靠，直接查注册表最准
    if item["id"] == "webview2":
        ok = webview2_version() is not None
    elif item["id"] == "netfx":
        ok = netfx_release() >= _NETFX_MIN_RELEASE
    else:
        ok = code in (0, 1638, 3010)

    log(f"{item['name']}：" + ("已就绪。" if ok else "仍未检测到，可能需要重启系统。"))
    return ok


# ---------------------------------------------------------------- 总入口

def _list_text(need_core: bool, pkgs: list[str], runtimes: list[dict]) -> str:
    lines = ["本软件运行需要以下几样东西，你电脑上还没有：", ""]
    if need_core:
        lines.append("  · Xray 代理内核（约 12 MB，从 GitHub 官方发布页下载）")
        lines.append("      没有它就没有代理，线路一条也起不来。")
    for spec in pkgs:
        lines.append(f"  · Python 库  {spec}")
    for item in runtimes:
        lines.append(f"  · {item['name']}（{item['size']}）")
        lines.append(f"      {item['why']}")
    lines += ["", "点「是」现在自动装好（需要联网），点「否」退出。"]
    return "\n".join(lines)


def ensure(interactive: bool = True, log=_log) -> bool:
    """启动前的总自检。

    返回 True  = 环境齐了，可以继续启动
    返回 False = 缺东西且用户拒绝 / 装失败，调用方应安静退出

    `interactive=False` 时只检查、不弹窗、不安装（给面板显示状态用）。
    """
    need_core = core_missing()
    pkgs_specs = [spec for _mod, spec in missing_packages()]
    runtimes = missing_runtimes()

    if not need_core and not pkgs_specs and not runtimes:
        return True

    if not interactive:
        return False

    if not _ask(_list_text(need_core, pkgs_specs, runtimes)):
        return False

    owned = _alloc_console("ChromeDual · 正在安装缺少的运行库")
    ok = True
    cache = Path(tempfile.gettempdir()) / "ChromeDual-install"
    try:
        log("=" * 58)
        log("ChromeDual 依赖安装")
        log("=" * 58)

        if need_core:
            ok = install_core(cache, log) and ok

        if pkgs_specs:
            ok = pip_install(pkgs_specs, log) and ok

        if runtimes:
            for item in runtimes:
                ok = install_runtime(item, cache, log) and ok

        log("")
        log("=" * 58)
        if ok:
            log("全部就绪。接下来会自动启动，这个黑窗口会自己关掉。")
        else:
            log("有一部分没装上，请看上面的提示。")
        log("=" * 58)
        _msgbox(
            "依赖已经装好了，即将启动。" if ok else
            "有项目没装上。\n请按控制台里的提示手动安装，或到项目 Issues 反馈。",
            "ChromeDual",
            _MB_ICONINFO if ok else _MB_ICONERROR,
        )
    finally:
        if owned:
            _free_console()

    if not ok:
        return False

    # 装完必须复检
    if core_missing() or missing_runtimes():
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """命令行入口：python -m app.deps [--check]"""
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        rep = report()
        lines = [
            "打包形态: " + ("是（exe）" if rep["frozen"] else "否（源码）"),
            "Python  : " + rep["python"],
            "内核    : " + (rep["core"] or "缺失"),
            "缺 pip  : " + ("、".join(rep["packages_missing"]) or "无"),
            "缺运行时: " + ("、".join(rep["runtimes_missing"]) or "无"),
            "WebView2: " + (rep["webview2"] or "未安装"),
            ".NET 4.x Release: " + str(rep["netfx_release"] or "未检测到"),
        ]
        # 打包成 exe 后没有控制台，print 到哪儿也看不见 —— 顺手写一份到日志目录，
        # 这样「双击没反应」的时候，至少有一个文件能看。
        try:
            from app import paths

            paths.ensure_dirs()
            out = paths.LOGS / "deps-check.txt"
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            lines.append("")
            lines.append("（以上内容已写入 " + str(out) + "）")
        except Exception:  # noqa: BLE001
            pass
        _log("\n".join(lines))
        return 0 if healthy() else 1
    return 0 if ensure(interactive=True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
