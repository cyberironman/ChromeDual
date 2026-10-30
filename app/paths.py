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
import shutil
import sys
from pathlib import Path

#: 是不是 PyInstaller 打包出来的 exe
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # 打包形态：exe 在哪，程序根就在哪（绿色版习惯，整个文件夹随拷随用）
    ROOT = Path(sys.executable).resolve().parent
    # 只读资源（assets / bin / config 里的 *.example.json / panel.html）
    # 被 PyInstaller 塞在临时解包目录里，和 exe 不是同一个地方
    RES = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    # 源码形态：app/paths.py 往上两层就是项目根
    ROOT = Path(__file__).resolve().parent.parent
    RES = ROOT


def _writable(path: Path) -> bool:
    """这个目录能不能写。

    ★ 不用「写一个探针文件再删掉」那种写法 —— 删除动作在受限环境里会被拦，
      直接把启动流程搞崩。`os.access(W_OK)` 无副作用。
    """
    try:
        return path.is_dir() and os.access(path, os.W_OK)
    except OSError:
        return False


def _resolve_data_root() -> Path:
    """用户数据（资料目录、日志、备份、配置）放哪。

    优先级：
      1. 环境变量 CHROMEDUAL_HOME —— 给「数据放别的盘」的人留的开关
      2. exe 所在目录可写 → 就放旁边（绿色版：U 盘、桌面随手拷）
      3. 装到了 Program Files 这类只读位置 → %LOCALAPPDATA%\\ChromeDual

    ★ 源码形态下 ROOT 一定可写，所以行为和以前完全一致。
    """
    env = os.environ.get("CHROMEDUAL_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if _writable(ROOT):
        return ROOT
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ChromeDual"


DATA_ROOT = _resolve_data_root()

APP = RES / "app"                      # 代码 + panel.html（打包形态在只读资源里）
CONFIG = DATA_ROOT / "config"          # 用户自己的配置（可写）
CONFIG_RES = RES / "config"            # 仓库里的模板（只读）
BIN = RES / "bin"                      # 程序自带的只读副本（打包形态在 _internal 里）
BIN_DATA = DATA_ROOT / "bin"           # 运行期真正用的内核（可写、可换版本、可下载）
ASSETS = RES / "assets"
DATA = DATA_ROOT / "data"
VAR = DATA_ROOT / "var"
TOOLS = RES / "tools"

PROFILES = CONFIG / "profiles.json"
PROFILES_EXAMPLE = CONFIG_RES / "profiles.example.json"
TARGETS = CONFIG / "targets.json"
TARGETS_EXAMPLE = CONFIG_RES / "targets.example.json"
SYSDEFAULT = CONFIG / "sysdefault.json"
SYSDEFAULT_EXAMPLE = CONFIG_RES / "sysdefault.example.json"

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

# 内核：一律指向**可写**位置。打包形态下 _internal\\bin 是只读资源，
# 不能用它当运行内核 —— 换版本、下载、权限都会卡在那儿。
# 首次启动时 ensure_core() 会把自带的那份复制过来。
XRAY = BIN_DATA / "xray.exe"

# 主入口：源码形态是 .py；打包形态就是 exe 自己。
# 外面一律通过 app/selfrun.py 来「再拉起自己」，不要直接拿 ENTRY 拼命令行 ——
# 源码形态前面得挂 pythonw.exe，打包形态绝不能挂，混了就会变成「找不到文件」。
if FROZEN:
    ENTRY = Path(sys.executable).resolve()
else:
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
    """启动时把必需的可写目录建出来（幂等）。

    注意只建 DATA_ROOT 底下的 —— ASSETS / BIN 是程序自带的只读资源，
    打包形态下压根不在 exe 旁边，去那儿 mkdir 只会白白报错。
    """
    for d in (CONFIG, DATA, CHROME_DATA, SOURCE_DATA, BIN_DATA,
              VAR, LOGS, BACKUPS, RUNTIME, RUN, STATE):
        d.mkdir(parents=True, exist_ok=True)


def _seed(target: Path, template: Path) -> bool:
    """模板 → 用户配置。已存在就不动（绝不覆盖用户改过的东西）。"""
    if target.is_file() or not template.is_file():
        return False
    CONFIG.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def ensure_profiles() -> bool:
    """第一次运行：把 config/*.example.json 变成真正的 config/*.json。

    仓库里只放模板（没有任何个人信息），真实配置在本地生成，
    并且已经写进 .gitignore —— 你的订阅地址、网址列表不会跟着代码传上去。
    返回 True 表示「这次是新生成的」，调用方可以据此提示用户。
    """
    created = _seed(PROFILES, PROFILES_EXAMPLE)
    _seed(TARGETS, TARGETS_EXAMPLE)
    _seed(SYSDEFAULT, SYSDEFAULT_EXAMPLE)
    return created


def ensure_core() -> bool:
    """把内核 xray.exe 就位到可写位置。返回 True = 现在有内核能用。

    三种情形：
      1. 可写位置已经有了 → 直接用（用户自己换过版本也认）
      2. 程序自带了一份（打包形态必然带 / 源码 clone 时也可能带）→ 复制过来
      3. 都没有（从 GitHub 下载的源码没有 bin/*.exe，因为太大了不进仓库）
         → 返回 False，由 app/deps.py 负责下载，或提示用户手动放
    """
    try:
        if XRAY.is_file():
            return True
        src = BIN / "xray.exe"
        if not src.is_file():
            return False
        BIN_DATA.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, XRAY)
        try:
            os.chmod(XRAY, 0o755)
        except OSError:
            pass
        return XRAY.is_file()
    except OSError:
        return False


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
