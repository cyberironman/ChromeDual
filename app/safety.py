# -*- coding: utf-8 -*-
"""破坏性操作的统一门禁 —— 任何可能弄丢 Chrome 数据的动作都必须先过这里。

为什么要有这个模块
------------------
2026-10-29 / 10-30 两次「所有网站同时退出登录」，事后复盘发现共同点：
**动手之前没有一份可回滚的完整快照，也没有任何一处强制确认**。
操作本身有的是复制目录、有的是启动 Chrome，看起来都"很安全"，
但 Chrome 自己会删掉解不开的 v20 Cookie —— 从外部看就像被清空。

所以规则写成代码，而不是写在文档里靠人记：
    1. 目标资料正在被 Chrome 使用 → **拒绝**（读不到、写了还会被覆盖）；
    2. 先做一次完整快照（Cookies + Login Data + Local State 成对，外加 Preferences/
       Bookmarks/Web Data），快照失败 → **拒绝继续**；
    3. 交互环境必须手动输入 yes；非交互（脚本/工具会话）必须显式 --yes；
    4. 每一次都写 var\\logs\\destructive.log，谁在什么时候动了哪份资料、备份在哪。

用法：
    from app import safety
    bak = safety.guard("恢复登录态", line_id="love", udd=..., assume_yes=args.yes)
    # 抛 safety.Refused 就说明被拦下了，调用方把原因原样告诉用户即可
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from app import paths
from app.logging_setup import log

AUDIT_LOG = paths.LOGS / "destructive.log"
VERSION_FILE = paths.RUN / "chrome_version.txt"

# 除了 Cookies 三件套，这些是「丢了很疼、但复制很便宜」的：
EXTRA_ITEMS = [
    "Preferences",
    "Secure Preferences",
    "Bookmarks",
    "Web Data",
    "History",
]


class Refused(RuntimeError):
    """被安全门禁拦下。消息是给用户看的中文原句。"""


def _audit(text: str) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pid={os.getpid()} {text}\n")
    except Exception:  # noqa: BLE001
        pass
    log.info("[安全门禁] %s", text)


def chrome_busy(udd: str | os.PathLike) -> int | None:
    """资料目录是否正被 Chrome 使用。返回 PID 或 None。"""
    from app.procs import chrome_running_with
    try:
        return chrome_running_with(Path(udd))
    except Exception:  # noqa: BLE001
        return None


def full_snapshot(tag: str, line_id: str, udd: str | os.PathLike,
                  profile: str = "Default") -> Path | None:
    """完整快照：登录三件套（成对）+ 书签/偏好/密码库。

    登录三件套必须成对 —— 只存 Cookies 不存 Local State，恢复回去照样解不开。
    """
    from app import login_backup
    root = Path(udd)
    prof = root / (profile or "Default")
    dst = login_backup.snapshot(line_id or "manual", root, profile,
                               tag=tag, force=True)
    if dst is None:
        # snapshot 在「Chrome 正在跑」或「Cookies 不存在」时返回 None
        return None
    for name in EXTRA_ITEMS:
        src = prof / name
        if not src.is_file():
            continue
        try:
            out = dst / name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        except Exception as e:  # noqa: BLE001
            log.warning("[安全门禁] 附带文件 %s 备份失败：%s", name, e)
    return dst


def guard(action: str, *, line_id: str = "", udd: str | os.PathLike | None = None,
          profile: str = "Default", assume_yes: bool = False,
          need_stopped: bool = True, what: str = "") -> Path | None:
    """破坏性操作的守门人。通过则返回「操作前快照」目录，否则抛 Refused。

    参数
    ----
    action      : 给用户看的动作名，如「恢复登录态」
    udd         : 受影响的 Chrome 资料目录
    need_stopped: 是否要求该目录没有 Chrome 在跑（几乎总是 True）
    what        : 这次会改动哪些东西，用于打印与审计
    """
    if udd is None:
        raise Refused(f"{action}：没有指定资料目录，拒绝执行")

    root = Path(udd)
    if not root.is_dir():
        raise Refused(f"{action}：资料目录不存在（{root}）")

    if need_stopped:
        pid = chrome_busy(root)
        if pid:
            raise Refused(
                f"{action}被拒绝：该资料目录正被 Chrome 使用（PID={pid}）。\n"
                f"请先关掉那个窗口 —— 运行中读不到 Cookies，写进去也会被 Chrome 覆盖。")

    print(f"\n即将执行：{action}")
    print(f"  资料目录：{root}")
    print(f"  profile ：{profile}")
    if what:
        print(f"  会改动：{what}")
    print("  第一步：先做完整快照（失败就中止）")

    # 先判断「有没有资格继续」，再花时间做快照 —— 非交互又没 --yes 的，
    # 不该白拷一份备份出来。
    if not assume_yes and (not sys.stdin or not sys.stdin.isatty()):
        _audit(f"拒绝 {action}（非交互且未加 --yes） dir={root}")
        raise Refused(
            "拒绝执行：当前不是交互环境，必须在命令后面显式加 --yes 才允许改动 Chrome 数据。")

    bak = full_snapshot(f"{action}·操作前", line_id, root, profile)
    if bak is None:
        _audit(f"拒绝 {action}（快照失败） dir={root}")
        raise Refused(
            f"拒绝执行：{action} 前的快照没做成（Cookies 读不到或目录被占用）。\n"
            f"没有回滚点的破坏性操作一律不做 —— 请先关掉 Chrome 再试。")
    print(f"  快照已存：{bak}\n")

    if not assume_yes:
        ans = input("确认继续？这一步会覆盖上面那份资料，输入 yes 继续：").strip().lower()
        if ans not in ("yes", "y"):
            _audit(f"用户取消 {action} dir={root}")
            raise Refused("已取消，什么都没改。")

    _audit(f"{action} dir={root} profile={profile} backup={bak}")
    return bak


def placeholder_note(path: Path, text: str) -> None:
    """在占位文件旁边留一句人能看懂的话（不影响 Chrome 读取）。"""
    try:
        p = path.parent / (path.name + ".说明.txt")
        p.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def read_only(path: Path) -> None:
    """给文件加只读属性（Windows 上 DeleteFile 会因只读而失败）。"""
    try:
        os.chmod(path, 0o444)
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        import ctypes
        FILE_ATTRIBUTE_READONLY = 0x1
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_READONLY)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------ Chrome 版本监测
def chrome_version(exe: str | os.PathLike | None = None) -> str:
    """读 chrome.exe 的文件版本号。"""
    p = str(exe or paths.CHROME_EXE)
    try:
        import win32api
        info = win32api.GetFileVersionInfo(p, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return ""


def version_changed() -> tuple[str, str] | None:
    """Chrome 版本变了就返回 (旧, 新)，否则 None。

    为什么要管：**Chrome 大版本升级是「所有网站同时掉登录」的头号诱因** ——
    升级会换掉 Cookie 的应用绑定密钥，解不开的 v20 Cookie 被 Chrome 自己删掉，
    而且不报错。2026-10-28 从 151 升到 156 之后，第二天主资料的 2704 条 Cookie
    就只剩 12 条。所以升级后的第一次启动必须**先备份，再让用户开窗口**。
    """
    cur = chrome_version()
    if not cur:
        return None
    old = ""
    try:
        if VERSION_FILE.is_file():
            old = VERSION_FILE.read_text(encoding="utf-8").strip()
        VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        VERSION_FILE.write_text(cur, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    if old and old != cur:
        return old, cur
    return None
