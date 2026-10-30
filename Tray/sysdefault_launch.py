"""系统默认浏览器转接器 —— 把外部点开的链接交给指定线路的 Chrome 实例。

为什么需要它：
    Windows 启动默认浏览器时是「照注册表里的命令行 + URL」直接执行，
    不认快捷方式里的 --user-data-dir / --proxy-server 参数。
    所以注册一个自定义浏览器条目，命令指向本脚本，由这里补齐参数。

**走哪条线由 config\\sysdefault.json 决定**：
    {"line": "profile-a"}   ← 默认：点链接直接进那条线路已登录的窗口
    {"line": "direct"}      ← 直连资料 + PAC 分流

它做三件事：
    1. 优先把「带 URL 的开窗请求」交给管家（控制口 openurl:<线路>|<URL>），
       参数由 app/line.py 现算 —— 和你在面板上点出来的窗口**完全同一套**
       （同一个 --user-data-dir、同一个代理端口），所以点链接会直接落到
       那个已经登录的窗口里，不会另起一份资料。
    2. 管家没起来 / 控制口没应答 → 自己按同一套配置算参数，直接拉 chrome.exe。
    3. 代理内核没起来时按需只拉这一条（ensure:<线路>），绝不「全量启动」。

调用方式（由协议处理程序调用）：
    pythonw.exe sysdefault_launch.py "https://example.com/"
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 根目录由本文件位置推导（本文件在 <根>/Tray/ 下），搬到哪里都能跑
ROOT = Path(__file__).resolve().parent.parent
TRAY = ROOT / "Tray"
PAC_DIR = ROOT / "assets" / "pac"
LOG_DIR = ROOT / "var" / "logs"
CONFIG = ROOT / "config"

# 用「当前解释器」推导 pythonw.exe —— 不写死任何用户目录。
# 用户可能装在 C:\Python、D:\Python311、虚拟环境里，写死一个路径就只在自己机器上能用。
_PY = Path(sys.executable)
PYW = _PY.with_name("pythonw.exe") if _PY.with_name("pythonw.exe").is_file() else _PY

printable_guard = ROOT / "ChromeDual.py"
GUARD = printable_guard if printable_guard.is_file() else ROOT / "Chrome代理管家.py"

if str(ROOT) not in sys.path:                 # 复用 app/paths.py 的 Chrome 探测
    sys.path.insert(0, str(ROOT))
try:
    from app.paths import find_chrome as _find_chrome
    CHROME = _find_chrome()
except Exception:  # noqa: BLE001
    CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

PAC_NAME = "system.pac"
PORTS_FILE = ROOT / "var" / "run" / "ports.json"
GUARD_CMD_FILE = ROOT / "var" / "run" / "cmd.txt"   # 控制口忙死时的兜底通道
SYSDEFAULT_CFG = CONFIG / "sysdefault.json"
PROFILES_CFG = CONFIG / "profiles.json"


def _expand(v):
    """展开配置里的 ${ROOT} 占位符（与 app/paths.py 同一规则）。"""
    if isinstance(v, str):
        return v.replace("${ROOT}", str(ROOT)).replace("%ROOT%", str(ROOT))
    return v



def _slots() -> dict:
    """端口是随机分配的（app/ports.py 写 var\\run\\ports.json），每次现读。

    以前这里写死 10890 / 10898 / 10888 / 10895，端口一随机化就全指错；
    而且管家没起来时 ports.json 还不存在 —— 那就让 ensure_guard() 先把管
    家拉起来，再读一次。
    """
    try:
        return json.loads(PORTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _slot(name: str) -> int:
    try:
        return int(_slots().get(name) or 0)
    except (TypeError, ValueError):
        return 0


def pac_port() -> int:
    return _slot("pac")


def line_port(line_id: str) -> int:
    return _slot(f"line:{line_id}")


def guard_port() -> int:
    return _slot("ctl")


def pac_url() -> str:
    p = pac_port()
    return f"http://127.0.0.1:{p}/{PAC_NAME}" if p else ""

# 每条线一份独立资料目录。**不要在这里写死目录** —— 一律从 profiles.json 读，
# 否则改配置后转接器会打开另一份资料（那就是「登录忽然没了」的经典成因）。
START_PAGE = (ROOT / "assets" / "start" / "index.html").as_uri()   # 没有 URL 时的起始页


def first_line() -> str:
    """profiles.json 里的第一条线路（配置缺失时的兜底）。"""
    try:
        raw = json.loads(PROFILES_CFG.read_text(encoding="utf-8"))
        for item in raw.get("lines") or []:
            lid = str(item.get("id") or "").strip()
            if lid:
                return lid
    except Exception:  # noqa: BLE001
        pass
    return ""


def default_line() -> str:
    """config\\sysdefault.json 里配的线路；没配就自动用 profiles.json 的第一条。"""
    try:
        v = str(json.loads(SYSDEFAULT_CFG.read_text(encoding="utf-8")).get("line") or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return first_line()


def line_cfg(lid: str) -> dict:
    """从 profiles.json 取这条线的 Chrome 配置 —— 和 app/line.py 读同一份。

    这里**不重新实现**「目录怎么算」，直接照抄 chrome.user_data_dir /
    profile_directory，保证转接器起的窗口和管家点出来的是同一份资料。
    """
    udd, prof, pac, extra = "", "Default", False, []
    try:
        raw = json.loads(PROFILES_CFG.read_text(encoding="utf-8"))
        for item in raw.get("lines") or []:
            if (item.get("id") or "") != lid:
                continue
            ch = item.get("chrome") or {}
            udd = _expand(str(ch.get("user_data_dir") or "").strip())
            prof = str(ch.get("profile_directory") or "Default")
            pac = bool(ch.get("pac"))
            extra = list(ch.get("extra_args") or [])
            break
    except Exception:  # noqa: BLE001
        pass
    return {"user_data_dir": Path(udd) if udd else (ROOT / "data" / "chrome" / lid),
            "profile": prof, "use_pac": pac, "extra": extra}


def pac_lines() -> list[str]:
    """PAC 模板里那两条线路（优先 + 兜底），id 从 profiles.json 读，不写死。"""
    ids: list[str] = []
    try:
        raw = json.loads(PROFILES_CFG.read_text(encoding="utf-8"))
        ps = raw.get("pac_server") or {}
        for key in ("video_line", "default_line"):
            v = str(ps.get(key) or "").strip()
            if v and v not in ids:
                ids.append(v)
        if len(ids) < 2:                     # 没配就取前两条非 PAC 线路
            for item in raw.get("lines") or []:
                lid = str(item.get("id") or "").strip()
                if not lid or lid in ids:
                    continue
                if (item.get("chrome") or {}).get("pac"):
                    continue
                ids.append(lid)
                if len(ids) >= 2:
                    break
    except Exception:  # noqa: BLE001
        pass
    return ids

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def _setup_log() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("sysdefault")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(LOG_DIR / "sysdefault.log", maxBytes=512 * 1024,
                            backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                     "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    return log


log = _setup_log()


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pac_alive() -> bool:
    """不只是端口在，还要真能取到 pac 内容（防端口被别的服务占了）。

    必须绕开系统代理直连本机，否则系统代理开着时会取不到。
    """
    import urllib.request

    url = pac_url()
    if not url:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.5) as r:
            body = r.read(8000)
        return b"FindProxyForURL" in body
    except Exception:  # noqa: BLE001
        return False


def guard_command(cmd: str, timeout: float = 1.2, allow_file: bool = True) -> str | None:
    """给管家发一句话（ping / up / show）。

    两条通道：
      1) socket 控制口（快，只要管家线程被调度到就立刻回）
      2) 命令文件 cmd.txt（慢 1 秒，但 socket 忙死时它照样生效）
    实测过 socket 会"端口还在、就是没人 accept"（面板/WebView2 忙），
    所以 socket 失败一定要落到文件上，否则点链接时线路拉不起来。
    """
    try:
        port = guard_port()
        if not port:
            raise OSError("控制口还没分配")
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(cmd.encode())
            s.settimeout(timeout)
            reply = s.recv(32).decode("utf-8", "ignore").strip()
            if reply:
                return reply
    except OSError:
        pass

    if not allow_file:
        return None

    # 兜底：留一张纸条给管家
    try:
        GUARD_CMD_FILE.write_text(cmd, encoding="utf-8")
        return "queued"
    except OSError as e:
        log.warning("写命令文件失败：%s", e)
        return None


def spawn(args: list[str]) -> None:
    subprocess.Popen(args, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                     close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_guard() -> bool:
    """保证管家在跑（它负责起 PAC 服务和代理内核）。"""
    if guard_command("ping", allow_file=False) is not None:
        return True
    if not (GUARD.is_file() and PYW.is_file()):
        log.warning("管家程序不存在：%s", GUARD)
        return False

    log.info("管家没在跑，拉起来")
    try:
        spawn([str(PYW), str(GUARD)])
    except Exception as e:  # noqa: BLE001
        log.warning("拉起管家失败：%s", e)
        return False

    deadline = time.time() + 10
    while time.time() < deadline:
        if guard_command("ping", timeout=0.6, allow_file=False) is not None:
            log.info("管家已就绪")
            return True
        time.sleep(0.4)
    log.warning("管家 10 秒内没应答")
    return False


def ensure_pac(wait_sec: float = 20.0) -> bool:
    if pac_alive():
        return True

    ensured = ensure_guard()
    if ensured:
        log.info("让管家启动全部线路")
        log.info("管家回：%s", guard_command("up", timeout=3.0))

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if pac_alive():
            log.info("PAC 服务已就绪")
            return True
        time.sleep(0.4)

    # 兜底：自己顶一个静态服务（只保证 pac 能取到，不管代理内核）
    # 注意：不能用 `pythonw -m http.server` —— pythonw 下 sys.stderr 是 None，
    # 标准库 handler 的 log_message 会抛异常，表现是"连上就断、空回复"。
    # 改用 guard_core 的 --pac-only（自带 log_message 兜底）。
    log.warning("PAC 服务没等到，自己起一个静态服务兜底")
    try:
        spawn([str(PYW), str(TRAY / "guard_core.py"), "--pac-only"])
    except Exception as e:  # noqa: BLE001
        log.error("PAC 静态服务启动失败：%s", e)
        return False

    deadline = time.time() + 6
    while time.time() < deadline:
        if pac_alive():
            return True
        time.sleep(0.3)
    log.error("PAC 服务最终仍不可用，本次将不带 PAC 启动")
    return False


def ensure_proxy(lid: str, wait_sec: float = 15.0) -> bool:
    """确保**这条线**的内核在跑。

    以前是「任意一条线路活着就算过」，而且没起来就发 `up`
    让管家**全量启动**（违反按需原则）。现在只拉目标线路：`ensure:<线路>`。
    """
    port = line_port(lid)
    if port and port_open(port):
        return True

    log.info("[%s] 内核还没起，请管家单独拉这一条", lid)
    if guard_command(f"ensure:{lid}", timeout=6.0) is None:
        return False

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if port_open(line_port(lid) or port):
            log.info("[%s] 内核已就绪", lid)
            return True
        time.sleep(0.4)
    log.warning("[%s] 内核仍未就绪，照常开窗（这个窗口可能上不了网）", lid)
    return False


def _via_guard(lid: str, urls: list[str]) -> bool:
    """首选路径：让管家按它自己的逻辑开窗（参数永远和面板点出来的一致）。

    为什么要绕这一道而不是直接 Popen：
      * `--user-data-dir` / 代理端口 / extra_args 全部由 app/line.py 现算，
        转接器不用再抄一遍（抄就会漂，漂了用户就看到「登录没了」）；
      * 管家会先 `ensure_line()` 把内核拉起来，再开窗，顺序不会错。
    """
    if not urls:
        return False
    cmd = f"openurl:{lid}|{urls[0]}"
    reply = guard_command(cmd, timeout=6.0)
    if reply and reply.startswith("ok"):
        log.info("[%s] 已交给管家开窗：%s", lid, urls[0])
        return True
    log.info("[%s] 管家没接这个请求（回 %r），改由转接器自己开窗", lid, reply)
    return False


def launch(urls: list[str]) -> int:
    if not CHROME.is_file():
        log.error("找不到 chrome.exe：%s", CHROME)
        return 2

    lid = default_line()
    cfg = line_cfg(lid)

    # ① 先保证管家活着（它负责内核和 PAC）
    ensure_guard()

    # ② 只有 PAC 线路才需要 PAC 服务；代理线只要那一条内核
    has_pac = False
    if cfg["use_pac"]:
        has_pac = ensure_pac()
        # PAC 里两条线路是「一条优先、一条兜底」，有任意一条活着就行；
        # 两条都没起就让管家把优先的那条拉起来（线路 id 从配置读，不写死）。
        vids = pac_lines()
        if not any(port_open(line_port(v) or 0) for v in vids) and vids:
            ensure_proxy(vids[0])
    else:
        ensure_proxy(lid)

    # ③ 首选：交给管家开（一参数都不自己拼）
    if _via_guard(lid, urls):
        return 0

    # ④ 兜底：管家没应答时自己开，参数照 profiles.json 算
    args = [
        str(CHROME),
        "--hide-crash-restore-bubble",
        "--no-default-browser-check",
        "--no-first-run",
        f"--user-data-dir={cfg['user_data_dir']}",
        f"--profile-directory={cfg['profile']}",
    ]
    if has_pac:
        args.append(f"--proxy-pac-url={pac_url()}")
    else:
        port = line_port(lid) or _slot(f"line:{lid}")
        if port:
            args.append(f"--proxy-server=http://127.0.0.1:{port}")
    # ⚠ extra_args 里四条线都写着 --new-window（config\profiles.json）。
    #   点链接要的是「在当前窗口开新标签」，所以这里必须滤掉，
    #   否则每点一次链接就弹一个新窗口，很快就满屏。
    args += [a for a in cfg["extra"] if str(a).strip() != "--new-window"]

    # 没有 URL（点图标 / 任务栏图标）→ 直接给起始页。
    # 不能指望 Preferences 里的 session.startup_urls：实测 Chrome 155 启动时
    # 会把没有 mac 校验的 session 段整段丢弃，写进去等于没写。
    if not urls:
        urls = [START_PAGE]
    args += urls

    try:
        spawn(args)
    except Exception as e:  # noqa: BLE001
        log.error("启动 Chrome 失败：%s", e)
        return 3

    log.info("[%s] 转接器直接开窗：%s", lid, " ".join(args[1:]))
    return 0


def main() -> int:
    raw = [a for a in sys.argv[1:] if a.strip()]
    urls = [a for a in raw if not a.startswith("--")]
    flags = [a for a in raw if a.startswith("--")]

    # 没有 URL（比如任务栏右键"新建窗口"）就什么都不传 ——
    # 让 Chrome 自己按 Preferences 里的启动页打开，避免和启动页重复开两个标签
    if flags:
        log.info("忽略内部参数：%s", flags)

    return launch(urls)


if __name__ == "__main__":
    sys.exit(main())
