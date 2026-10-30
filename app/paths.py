# -*- coding: utf-8 -*-
"""唯一路径解析模块 —— 全项目只有这里知道「东西放哪」。

★ 设计原则：**任何路径都不写死**。
    根目录由本文件的位置推导（`app/paths.py` 往上两层），
    所以整个文件夹搬到任何地方（别的盘、U 盘、另一台机器）都能直接跑。

    配置文件里可以用 `${ROOT}` 这种占位符表示根目录，启动时现展开，
    这样既不用写死绝对路径，也不用手工改配置。

为什么单独抽出来：
    以前路径散在 3 个文件里（一个写死 Tray 目录，一个用 __file__，
    一个又写死备份根），结果同一份配置有两个来源、日志写到两个地方，
    排障要先猜文件在哪。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 包在 <根>\app\ 下，所以根是上两层
ROOT = Path(__file__).resolve().parent.parent

APP = ROOT / "app"
CONFIG = ROOT / "config"
BIN = ROOT / "bin"
ASSETS = ROOT / "assets"
DATA = ROOT / "data"
VAR = ROOT / "var"
TOOLS = ROOT / "tools"

PROFILES = CONFIG / "profiles.json"
PROFILES_EXAMPLE = CONFIG / "profiles.example.json"
TARGETS = CONFIG / "targets.json"
SYSDEFAULT = CONFIG / "sysdefault.json"

CHROME_DATA = DATA / "chrome"
SOURCE_DATA = DATA / "source"

LOGS = VAR / "logs"
BACKUPS = VAR / "backups"
RUNTIME = VAR / "runtime"
RUN = VAR / "run"
STATE = VAR / "state"
CMD_FILE = RUN / "cmd.txt"

ICON = ASSETS / "guard.ico"
ICON_ALT = ASSETS / "icons" / "guard.ico"
PAC_DIR = ASSETS / "pac"
START_PAGE = ASSETS / "start" / "index.html"

XRAY = BIN / "xray.exe"

# 主入口：优先英文名（跨平台/跨文件系统都稳），兼容旧的中文名
ENTRY = ROOT / "ChromeDual.py"
if not ENTRY.is_file():
    _alt = ROOT / "Chrome代理管家.py"
    if _alt.is_file():
        ENTRY = _alt


# ------------------------------------------------------------ Chrome 在哪
def find_chrome() -> Path:
    """找本机 Chrome，找不到就返回约定位置（启动时会给出明确报错）。

    顺序：环境变量 → 注册表 App Paths → 常见安装路径 → Application\\<版本>\\chrome.exe。
    """
    env = os.environ.get("CHROMEDUAL_CHROME")
    if env and Path(env).is_file():
        return Path(env)

    if os.name == "nt":
        try:
            import winreg

            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
                    try:
                        with winreg.OpenKey(
                                root,
                                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                                0, winreg.KEY_READ | view) as k:
                            p = winreg.QueryValueEx(k, "")[0]
                            if p and Path(p).is_file():
                                return Path(p)
                    except OSError:
                        continue
        except Exception:  # noqa: BLE001
            pass

    for cand in (
        os.environ.get("PROGRAMFILES", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
    ):
        if cand and Path(cand).is_file():
            return Path(cand)

    # 兜底：Application\<版本>\chrome.exe
    try:
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application"
        for d in sorted(base.glob("*"), reverse=True):
            exe = d / "chrome.exe"
            if exe.is_file():
                return exe
    except Exception:  # noqa: BLE001
        pass

    return Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


CHROME_EXE = find_chrome()


# ------------------------------------------------------------ 占位符展开
def expand(value):
    """把配置里的 `${ROOT}` / `${HOME}` 等占位符展开成真实路径。

    递归处理 dict / list / str，其它类型原样返回。
    这样配置模板可以被复制到任何机器上，不用手改一处路径。
    """
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    if not isinstance(value, str):
        return value

    mapping = {
        "ROOT": str(ROOT),
        "HOME": str(Path.home()),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "PYTHON": sys.executable,
    }
    out = value
    for key, val in mapping.items():
        out = out.replace("${" + key + "}", val).replace("%" + key + "%", val)
    return out


def ensure_dirs() -> None:
    """启动时把必需的目录建出来（幂等）。"""
    for d in (APP, CONFIG, BIN, ASSETS, DATA, CHROME_DATA, SOURCE_DATA,
              VAR, LOGS, BACKUPS, RUNTIME, RUN, STATE, PAC_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_profiles() -> bool:
    """第一次运行：把 config/profiles.example.json 变成 config/profiles.json。

    仓库里只放模板（没有任何个人信息），真实配置在本地生成，
    并且已经写进 .gitignore —— 你的订阅地址、网址列表不会跟着代码传上去。
    返回 True 表示「这次是新生成的」，调用方可以据此提示用户。
    """
    if PROFILES.is_file():
        return False
    if not PROFILES_EXAMPLE.is_file():
        return False
    CONFIG.mkdir(parents=True, exist_ok=True)
    PROFILES.write_text(PROFILES_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return True


# ---------------------------------------------------------------- 按线路取目录
def chrome_dir(line_id: str) -> Path:
    """该线路**独占**的 Chrome 资料目录（= --user-data-dir 的值）。

    ★ 每条线路一个目录，是「账户互不干扰」的物理基础。
      任何一条线都不允许再用系统默认的 %LOCALAPPDATA%\\Google\\Chrome\\User Data。
    """
    return CHROME_DATA / line_id


def source_dir(line_id: str) -> Path:
    """该线路的订阅 / 节点池 / 探测缓存。"""
    return SOURCE_DATA / line_id


def runtime_dir(line_id: str) -> Path:
    """该线路运行期生成的内核配置。"""
    return RUNTIME / line_id


def backup_dir(line_id: str) -> Path:
    """该线路的登录态备份（**按线路隔离**，A 线的备份永远看不到 B 线的）。"""
    return BACKUPS / line_id


def line_log(line_id: str) -> Path:
    return LOGS / f"{line_id}.log"


def line_core_log(line_id: str) -> Path:
    """内核自己写的日志。"""
    return LOGS / f"{line_id}-core.log"


def system_user_data_dir() -> Path:
    """系统默认的 Chrome 资料目录。

    ⚠ 只用于「迁移旧数据」和「体检对比」，**运行期任何线路都不许挂在它上面**。
    """
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
