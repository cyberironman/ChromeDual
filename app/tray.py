# -*- coding: utf-8 -*-
"""托盘程序：图标 + 菜单 + 单实例 + 控制口 + 开机自启。

只做「界面与入口」，业务在 guard.py，界面在 panel.py。
它不碰 Windows 系统代理，所以和 v2rayN / Codex 管家不打架。
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from app import paths, ports, selfrun
from app.guard import Guard
from app.logging_setup import log, setup_logging

CMD_FILE = paths.CMD_FILE              # 命令文件兜底通道

C_OK = (34, 160, 90)
C_WARN = (230, 160, 30)
C_BAD = (200, 60, 60)
C_IDLE = (130, 130, 130)

# pystray 的 _util.win32 没导出 WM_LBUTTONDBLCLK（只有 UP/DOWN/RUP），自己写死
WM_LBUTTONDBLCLK = 0x0203


def _ping(port: int, timeout: float = 0.8) -> str:
    """往某个端口发 ping，返回对端回的字（没有回/没监听 → 空串）。

    单实例判断和 cli.py 都靠它 —— 端口是随机的，只能「问了才知道是不是自家管家」。
    """
    if not port:
        return ""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout) as s:
            s.sendall(b"ping")
            s.settimeout(timeout)
            return s.recv(16).decode("utf-8", "ignore").strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- 图标
def make_icon(color: tuple[int, int, int]) -> Image.Image:
    """托盘图标：整块就是状态色，中间一个白点。

    以前是「深色底 + 白圆环」，缩到 16px 时白色占了大半，红/绿根本看不出来，
    所以整块改成状态色 —— 缩小后颜色仍然一眼可辨。
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=12,
                        fill=color, outline=(255, 255, 255), width=3)
    d.ellipse((25, 25, size - 26, size - 26), fill=(255, 255, 255))
    return img


def make_icon_class():
    """返回「左键单击 = 打开控制面板、右键 = 菜单」的图标类。

    ⚠ 以前设计成「单击弹菜单、双击开面板」，但**双击根本不会触发**：
    pystray 注册窗口类时写死 `style=0`，没有 `CS_DBLCLKS`，Windows 就不会产生
    WM_LBUTTONDBLCLK —— 那段双击分支是死代码，用户点图标只看到菜单、
    永远打不开面板（2026-10-29 那次故障的直接原因之一）。
    """
    try:
        from pystray import _win32 as W
    except Exception:  # noqa: BLE001
        return pystray.Icon

    class LeftClickIcon(W.Icon):
        handler = None        # 由 App 挂上来：单击图标要干的事（打开控制面板）

        def _on_notify(self, wparam, lparam):  # noqa: N802
            win32 = W.win32
            if lparam in (win32.WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                if callable(self.handler):
                    threading.Thread(target=self.handler, daemon=True).start()
                return
            return super()._on_notify(wparam, lparam)

    return LeftClickIcon


TRAY_ICON_CLS = make_icon_class()


# --------------------------------------------------------------------------- 应用
class App:
    # pywebview 遍历 js_api 属性图的哨兵（见 panel.py 里 Api 的注释）
    _serializable = False

    def __init__(self) -> None:
        self.guard = Guard()
        self.icon: pystray.Icon | None = None
        self.panel = None
        self._lock_sock: socket.socket | None = None
        self.control_port: int = 0      # 由 acquire_single() 随机分配
        self._color = C_IDLE
        self._busy = False
        self._quit = threading.Event()

    # -- 单实例 -------------------------------------------------------------
    def acquire_single(self) -> bool:
        """单实例锁 + 控制口。端口**随机分配并持久化**（见 app/ports.py），
        不再写死 10895 —— 三个代理软件挤在 108xx 段是 10-29 那场事故的根因。

        顺序很关键：先看「上次那个控制口是不是还活着」。
        活着 = 已经有实例在跑 → 直接返回 False 退出；
        绝不能一上来就重新随机一个端口自己绑，否则两个实例各绑各的。
        """
        prev = int(ports.load_state().get("ctl") or 0)

        def alive(port: int) -> bool:
            return _ping(port) == "pong"

        port = 0
        try:
            port = int(ports.assign({"ctl": prev}, ours=alive, paired=())["ctl"])
        except Exception as e:  # noqa: BLE001
            log.error("控制口分配失败：%s", e)
        if not port:
            return False
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.listen(8)
            self._lock_sock = s
            self.control_port = port
            return True
        except OSError:
            s.close()
            return False

    # -- 控制口 -------------------------------------------------------------
    #   up   -> 启动全部线路（已在跑的会跳过）
    #   ping -> 只回 pong
    #   show -> 打开面板
    #   quit -> 正常退出（先停内核）
    #   open:<id>        -> 用该线路打开 Chrome
    #   opentarget:<id>  -> 用「当前实测能打开该站点」的线路打开
    def handle_cmd(self, cmd: str) -> str:
        # ⚠ 判断用 lower()，**取参数用原文**。
        #   以前整条指令在 _serve_control 里就被 lower 了，于是
        #   `openurl:profile-a|https://a.com/AbC` 的 URL 会被压成小写，
        #   路径区分大小写的站点会 404。指令名不区分大小写，URL 必须保留原样。
        lc = cmd.lower()
        if lc == "up":
            self._bg(self.act_start)
            return "ok"
        if lc == "show":
            threading.Thread(target=self.act_show, daemon=True).start()
            return "ok"
        if lc == "ping":
            return "pong"
        if lc == "quit":
            threading.Thread(target=self.act_quit, daemon=True).start()
            return "bye"
        if lc.startswith("opentarget:"):
            tid = cmd.split(":", 1)[1].strip()
            if tid in {t["id"] for t in self.guard.targets}:
                self.act_open_target(tid)
                return "ok"
            return "unknown-target"
        if lc.startswith("openurl:"):
            # 系统默认浏览器转接器发来的：openurl:<线路>|<URL>
            rest = cmd.split(":", 1)[1].strip()
            lid, _, url = rest.partition("|")
            lid, url = lid.strip().lower(), url.strip()
            if self.guard.line(lid) is not None and url:
                self.act_open_url(lid, url)
                return "ok"
            return "unknown-line"
        if lc.startswith("ensure:"):
            # 只要内核起来，不开窗口（转接器 / 托盘按需拉起用）
            lid = cmd.split(":", 1)[1].strip().lower()
            if self.guard.line(lid) is None:
                return "unknown-line"
            ok = self.guard.ensure_line(lid)
            return "ok" if ok else "fail"
        if lc.startswith("open:"):
            lid = cmd.split(":", 1)[1].strip().lower()
            if self.guard.line(lid) is not None:
                self.act_open(lid)
                return "ok"
            return "unknown-line"
        return "unknown"

    def _serve_control(self, sock: socket.socket) -> None:
        # Windows 上「accept() + settimeout」有坑：accept 超时一次之后这个监听
        # socket 就废了 —— 外面看到端口还 LISTENING，连上去却 Connection refused。
        # 所以改成 select 等可读，再 accept。
        import select
        import traceback

        log.info("控制口线程已启动（127.0.0.1:%s）", self.control_port or "未分配")
        try:
            while True:
                try:
                    ready, _, _ = select.select([sock], [], [], 1.0)
                except OSError:
                    log.warning("控制口 select 失败，线程退出")
                    return
                if not ready:
                    continue
                try:
                    conn, _ = sock.accept()
                except OSError as e:
                    log.warning("控制口 accept 失败：%s", e)
                    continue
                try:
                    conn.settimeout(0.8)
                    # 4096 字节：要装得下一条完整 URL（以前只有 64，长链接会被截断）。
                    # **不做 lower()** —— URL 的路径区分大小写，压成小写会 404。
                    cmd = conn.recv(4096).decode("utf-8", "ignore").strip()
                    log.info("控制口收到指令：%r", cmd)
                    conn.sendall((self.handle_cmd(cmd) + "\n").encode())
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        except BaseException:  # noqa: BLE001
            # pythonw 下 sys.stderr 是 None，线程里的异常会彻底静默，
            # 只能自己写进日志（之前就是这样"端口在、但没人应答"）。
            log.error("控制口线程异常退出：\n%s", traceback.format_exc())
            raise

    def _watch_cmd_file(self) -> None:
        """命令文件通道 —— socket 偶尔不响应时的兜底。

        实测：面板/WebView2 忙的时候，控制口的连接会排在 backlog 里没人 accept
        （端口还 LISTENING，连上去却一直没回音）。写文件反而必定生效。
        """
        paths.RUN.mkdir(parents=True, exist_ok=True)
        log.info("命令文件通道已就绪：%s", CMD_FILE)
        while True:
            try:
                if CMD_FILE.is_file():
                    txt = CMD_FILE.read_text(encoding="utf-8", errors="ignore").strip().lower()
                    try:
                        CMD_FILE.unlink()
                    except OSError:
                        pass
                    if txt:
                        log.info("命令文件：%r", txt)
                        self.handle_cmd(txt)
            except Exception:  # noqa: BLE001
                log.debug("命令文件处理失败", exc_info=True)
            time.sleep(1.0)

    # -- 状态 ---------------------------------------------------------------
    def summarize(self) -> tuple[tuple[int, int, int], str]:
        snap = self.guard.snapshot()
        # 按需模式：没开窗口时一个内核都不跑，这是**正常待机**，不是故障。
        active = [s for s in snap if s.get("desired") or s.get("windows")]
        oks = [s["ok"] for s in snap]
        good, total = sum(oks), len(oks)
        if not active and good == 0:
            return C_IDLE, "待机（开窗口时才起代理）"
        if good == total and total:
            return C_OK, f"全部就绪（{good}/{total}）"
        if good == 0:
            return C_BAD, f"全部未就绪（0/{total}）"
        return C_WARN, f"部分就绪（{good}/{total}）"

    def refresh(self) -> None:
        color, text = self.summarize()
        if self.icon is not None:
            if color != self._color:
                self._color = color
                self.icon.icon = make_icon(color)
            self.icon.title = "Chrome 代理管家 — " + text
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    # -- 后台动作 -----------------------------------------------------------
    def _bg(self, fn, *a, **kw) -> None:
        def run() -> None:
            if self._busy:
                log.warning("上一个操作还没结束，忽略本次请求")
                return
            self._busy = True
            try:
                fn(*a, **kw)
            except Exception:  # noqa: BLE001
                log.exception("操作异常")
            finally:
                self._busy = False
                self.refresh()

        threading.Thread(target=run, daemon=True).start()

    def act_start(self) -> None:
        log.info("== 手动：启动全部线路 ==")
        self.guard.start_all()

    def act_restart(self) -> None:
        log.info("== 手动：重建配置并重启全部线路 ==")
        self.guard.restart_all()

    def act_stop(self) -> None:
        log.info("== 手动：停止全部线路 ==")
        self.guard.stop_all()

    def act_open(self, lid: str) -> None:
        line = self.guard.line(lid)
        if line is None:
            return

        def run() -> None:
            # ★ 按需：只有这条线要开窗口，才拉它自己的内核（其他线路完全不动）
            ok = self.guard.ensure_line(lid)
            if not ok and not line.use_pac:
                log.warning("[%s] 内核没能起来，窗口照样开（可能上不了网）", line.name)
            msg = line.open_chrome(self.guard.chrome_exe)
            log.info(msg)
            self._notify("Chrome 代理管家", msg)

        self._bg(run)

    def act_open_url(self, lid: str, url: str) -> None:
        """系统默认浏览器点进来的链接：交给这条线的窗口打开。

        不弹系统通知 —— 点个链接就冒一个气泡太吵，只写日志。
        窗口已经开着时 Chrome 会把 URL 转交给已有实例，
        所以这里照常走 open_chrome(url=...) 就行，不会另起一份资料。
        """
        line = self.guard.line(lid)
        if line is None:
            return

        def run() -> None:
            self.guard.ensure_line(lid)
            # new_window=False：窗口已开着就在里面开新标签，别每点一次弹一个新窗口
            msg = line.open_chrome(self.guard.chrome_exe, url=url, new_window=False)
            log.info(msg)

        self._bg(run)

    def act_open_target(self, tid: str) -> None:
        def run() -> None:
            log.info("== 按重点网址打开：%s ==", tid)
            msg = self.guard.open_target(tid)
            log.info(msg)
            self._notify("Chrome 代理管家", msg)

        self._bg(run)

    def act_check(self) -> None:
        def run() -> None:
            for line in self.guard.lines:
                st = line.status()
                if st["state"] != "running":
                    log.info("[检查] %s：%s", line.name, st["detail"])
                    continue
                ok = line.health(self.guard.health_url)
                log.info("[检查] %s：%s（%s）", line.name, "通" if ok else "不通",
                         line.last_check_text)

        self._bg(run)

    # -- 控制面板 -----------------------------------------------------------
    def act_show(self) -> None:
        if self.panel is not None and self.panel.show():
            log.info("控制面板已显示")
            return
        log.warning("控制面板不可用（检查 pywebview / WebView2 运行时）")
        self._notify("Chrome 代理管家", "控制面板打不开，请看 var\\logs\\guard.log")

    def panel_status(self) -> dict:
        """给页面轮询用的状态快照（全部是本地端口探测，很快）。"""
        snap = self.guard.snapshot()
        oks = [s["ok"] for s in snap]
        good, total = sum(oks), len(oks)
        active = [s for s in snap if s.get("desired") or s.get("windows")]
        if not active and good == 0:
            # 按需模式下的正常待机：不算故障，用灰点
            level, text = "idle", "待机中 · 开窗口时才启动对应代理"
        elif total and good == total:
            level, text = "ok", f"全部就绪（{good}/{total}）"
        elif good == 0:
            level, text = "bad", f"全部未就绪（0/{total}）"
        else:
            level, text = "warn", f"部分就绪（{good}/{total}）"
        notes = []
        n_orphan = sum(1 for s in snap if s.get("state") == "orphan")
        n_foreign = sum(1 for s in snap if s.get("state") == "foreign")
        n_win = sum(1 for s in snap if s.get("windows"))
        if n_orphan:
            notes.append(f"{n_orphan} 条是遗留内核")
        if n_foreign:
            notes.append(f"{n_foreign} 条端口被其他程序占")
        if notes:
            text += "，" + "、".join(notes)
            if level == "ok":
                level = "warn"
        if n_win:
            text += f"；{n_win} 条线路有 Chrome 窗口"
        return {
            "ok": bool(total and good == total),
            "level": level,
            "text": text,
            "lines": snap,
            "targets": self.guard.targets_status(),
            "autostart": self.autostart_on(),
            "busy": bool(self._busy),
            "root": str(paths.ROOT),
        }

    def panel_action(self, name: str, arg=None) -> dict:
        """页面按钮的统一入口。耗时动作一律丢后台线程，立刻回一句提示。"""
        key = (name or "").strip().lower()
        if key == "start":
            self._bg(self.act_start)
            return {"ok": True, "msg": "正在启动全部线路…"}
        if key == "stop":
            self._bg(self.act_stop)
            return {"ok": True, "msg": "正在停止全部线路…"}
        if key == "restart":
            self._bg(self.act_restart)
            return {"ok": True, "msg": "正在重新测速并重建线路（约 1 分钟）"}
        if key == "check":
            self.act_check()
            return {"ok": True, "msg": "正在检查，结果写到日志里"}
        if key == "open":
            self.act_open(str(arg or ""))
            return {"ok": True, "msg": "正在打开 Chrome…"}
        if key == "adopt":
            line = self.guard.line(str(arg or ""))
            if line is None:
                return {"ok": False, "msg": "找不到该线路"}
            if line.use_pac:
                return {"ok": False, "msg": "PAC 线路没有内核可接管"}
            pids = line.managed_pids()
            if not pids:
                return {"ok": False, "msg": "没找到遗留内核，点「启动」重新拉起即可"}
            line.adopt(pids)
            return {"ok": True, "msg": f"已接管遗留内核 PID {', '.join(map(str, pids))}"}
        if key == "opentarget":
            self.act_open_target(str(arg or ""))
            return {"ok": True, "msg": "正在挑一条能打开该网站的线路…"}
        if key == "autostart":
            self.act_toggle_autostart()
            on = self.autostart_on()
            return {"ok": True, "msg": "已开启开机自启" if on else "已关闭开机自启"}
        if key == "openlog":
            self.act_open_log()
            return {"ok": True, "msg": "已打开管家日志"}
        if key == "opendir":
            self.act_open_dir()
            return {"ok": True, "msg": "已打开程序文件夹"}
        if key == "opendata":
            os.startfile(str(paths.DATA))  # noqa: S606
            return {"ok": True, "msg": "已打开数据文件夹"}
        if key == "quit":
            threading.Thread(target=self.act_quit, daemon=True).start()
            return {"ok": True, "msg": "正在退出…"}
        return {"ok": False, "msg": f"未知操作：{name}"}

    # -- 打开目录/日志 -------------------------------------------------------
    def act_open_log(self) -> None:
        p = paths.LOGS / "guard.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["notepad.exe", str(p)], creationflags=0x08000000)

    def act_open_line_log(self, lid: str) -> None:
        p = paths.line_log(lid)
        if not p.exists():
            p = paths.line_core_log(lid)
        if p.exists():
            subprocess.Popen(["notepad.exe", str(p)], creationflags=0x08000000)

    def act_open_dir(self) -> None:
        os.startfile(str(paths.ROOT))  # noqa: S606

    # -- 开机自启 -----------------------------------------------------------
    @staticmethod
    def _startup_dir() -> Path:
        return Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"

    def autostart_on(self) -> bool:
        return (self._startup_dir() / "Chrome代理管家.lnk").exists()

    def act_toggle_autostart(self) -> None:
        lnk = self._startup_dir() / "Chrome代理管家.lnk"
        if lnk.exists():
            lnk.unlink()
            log.info("已关闭开机自启")
            self._notify("Chrome 代理管家", "已关闭开机自启")
            return
        try:
            # 必须用 WScript.Shell 建 lnk：pylnk3 的 for_file() 写死 link_info=None，
            # 生成的 lnk 没有 LinkInfo 段，资源管理器定位会直接打开 System32（踩过）。
            import win32com.client
            exe, argv, workdir = selfrun.shortcut_target()
            sh = win32com.client.Dispatch("WScript.Shell")
            item = sh.CreateShortcut(str(lnk))
            item.TargetPath = exe
            item.Arguments = argv
            item.WorkingDirectory = workdir
            ico = paths.ICON if paths.ICON.is_file() else paths.ICON_ALT
            item.IconLocation = (str(ico) if ico.is_file() else str(exe)) + ",0"
            item.Description = "Chrome 代理管家（托盘）"
            item.Save()
            back = sh.CreateShortcut(str(lnk)).TargetPath
            if back.lower() != str(exe).lower():
                raise RuntimeError(f"快捷方式校验失败：读回 {back!r}")
            log.info("已开启开机自启：%s", lnk)
            self._notify("Chrome 代理管家", "已开启开机自启")
        except Exception as e:  # noqa: BLE001
            log.exception("创建自启快捷方式失败：%s", e)
            self._notify("Chrome 代理管家", f"开启自启失败：{e}")

    # -- 通知 ---------------------------------------------------------------
    def _notify(self, title: str, msg: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(msg, title)
        except Exception:  # noqa: BLE001
            pass

    def act_quit(self) -> None:
        log.info("退出管家")
        # ★ 必须先把内核带走。以前退出什么都不杀，于是每退一次就落下一批孤儿 xray，
        #   下次启动又「端口通则复用」，面板四条线全绿、其实全是不归自己管的旧进程。
        try:
            self.guard.stop_all()
            log.info("已停止全部线路")
        except Exception as e:  # noqa: BLE001
            log.exception("退出时停止线路失败：%s", e)
        if self.panel is not None:
            self.panel.destroy()      # 先关面板，webview.start() 才会返回
        try:
            if self.icon is not None:
                self.icon.stop()
        finally:
            self._quit.set()
            if self._lock_sock is not None:
                try:
                    self._lock_sock.close()
                except Exception:  # noqa: BLE001
                    pass
        # 兜底：面板是从后台线程销毁的，pywebview 的 WinForms 后端不一定让
        # webview.start() 返回（实测退出后内核全停了、进程却还挂着）。
        # 内核此时已经全部停掉、锁也关了，硬退是安全的。
        threading.Timer(2.5, lambda: os._exit(0)).start()

    # -- 菜单 ---------------------------------------------------------------
    def build_menu(self) -> pystray.Menu:
        def status_item(line) -> pystray.MenuItem:
            def text(_item) -> str:
                st = line.status()
                # Win32 原生菜单不渲染彩色 emoji（🟢 会变成黑圈），只能用文字标状态。
                mark = {"running": "[通]", "starting": "[起]", "foreign": "[占]",
                        "orphan": "[遗]"}.get(st["state"], "[断]")
                port = line.chrome.get("pac_port") or line.http_port
                return f"{mark} {line.name}（{port}）"

            return pystray.MenuItem(text, lambda: self.act_open(line.id))

        line_items = [status_item(l) for l in self.guard.lines]

        def autostart_text(_item) -> str:
            return ("✓ 开机自启" if self.autostart_on() else "　开机自启")

        return pystray.Menu(
            pystray.MenuItem(lambda _: "打开控制面板（单击图标）", lambda: self.act_show()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "— 点线路名直接开对应 Chrome —", None, enabled=False),
            *line_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "启动全部线路", lambda: self._bg(self.act_start)),
            pystray.MenuItem(lambda _: "⚡ 测速优化并重启（约 1 分钟）",
                             lambda: self._bg(self.act_restart)),
            pystray.MenuItem(lambda _: "停止全部线路", lambda: self._bg(self.act_stop)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "检查线路连通性", lambda: self.act_check()),
            pystray.MenuItem(autostart_text, lambda: self.act_toggle_autostart()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "打开管家日志", lambda: self.act_open_log()),
            pystray.MenuItem(lambda _: "打开数据文件夹", lambda: self.panel_action("opendata")),
            pystray.MenuItem(lambda _: "打开程序文件夹", lambda: self.act_open_dir()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "退出", lambda: self.act_quit()),
        )

    # -- Chrome 升级监测 -----------------------------------------------------
    def _check_chrome_upgrade(self) -> None:
        """Chrome 版本一变，先把每份资料完整备份一遍，再提醒用户。

        这是唯一「事前」而不是「事后」的保护：Cookie 被 Chrome 删掉之后，
        文件层面救不回来（v20 绑了资料目录路径，服务端会话也会被判失效）。
        """
        from app import safety
        try:
            ch = safety.version_changed()
        except Exception as e:  # noqa: BLE001
            log.debug("Chrome 版本检测失败：%s", e)
            return
        if not ch:
            return
        old, new = ch
        log.warning("检测到 Chrome 升级：%s -> %s，先给每份资料留快照", old, new)
        done = 0
        for line in self.guard.lines:
            try:
                bak = safety.full_snapshot(f"Chrome升级 {old}→{new}", line.id,
                                           line.user_data_dir, line.profile_directory)
                if bak:
                    done += 1
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 升级前快照失败：%s", line.name, e)
        self._notify(
            "Chrome 代理管家",
            f"Chrome 从 {old} 升级到 {new}。已给 {done} 份资料留了登录态快照 —— "
            f"升级可能让网站掉登录，掉了就用「tools\\恢复登录状态.py」回滚。")

    def _delayed_start(self) -> None:
        """管家起来的默认姿态：**只待机，不预热任何内核**。

        以前这里调 start_all()，结果软件一开机就把三条内核全拉起来、
        而对应的 Chrome 一个都没开（用户看到的就是「先开了一堆代理」）。
        现在改成「开哪个窗口才起哪条线」，空闲后自动回收。
        """
        self.guard.start_passive()

    def _watch_status(self, interval: float = 15.0) -> None:
        log.info("状态巡检线程已启动（%.0f 秒一轮）", interval)
        while True:
            time.sleep(interval)
            try:
                self.refresh()
                log.debug("状态巡检完成（%.0fs 一轮）", interval)
            except Exception:  # noqa: BLE001
                log.debug("状态刷新失败", exc_info=True)

    def run(self) -> None:
        if not self.acquire_single():
            log.warning("已经有一个管家在运行，本次退出（单实例）")
            return
        threading.Thread(target=self._serve_control, args=(self._lock_sock,),
                         name="control", daemon=True).start()
        threading.Thread(target=self._watch_cmd_file, name="cmd-file", daemon=True).start()

        color, text = self.summarize()
        self._color = color
        self.icon = TRAY_ICON_CLS("chrome-guard", make_icon(color),
                                  "Chrome 代理管家 — " + text, self.build_menu())
        self.icon.handler = self.act_show      # 单击图标 → 打开控制面板

        # Chrome 升级后的第一次启动：先把四份资料各备份一份，再让用户开窗口。
        threading.Thread(target=self._check_chrome_upgrade, name="chrome-ver",
                         daemon=True).start()

        # 控制面板先创建（隐藏），失败就退回纯托盘模式，绝不影响托盘
        try:
            from app.panel import Panel
            candidate = Panel(self)
            self.panel = candidate if candidate.create() else None
        except Exception:  # noqa: BLE001
            log.exception("控制面板初始化失败，退回纯托盘模式")
            self.panel = None

        # 托盘放后台线程，主线程让给 webview 的消息循环（WebView2 要求主线程）
        try:
            self.icon.run_detached()
        except Exception:  # noqa: BLE001
            log.exception("托盘线程启动失败")

        try:
            threading.Thread(target=self._delayed_start, name="autostart",
                             daemon=True).start()   # 启动即拉线路（不占「忙碌」标记）
        except Exception:  # noqa: BLE001
            log.exception("启动线路失败")
        threading.Thread(target=self._watch_status, name="status-watch",
                         daemon=True).start()

        if self.panel is not None:
            try:
                self.panel.start()      # 阻塞；只有 act_quit() 销毁窗口才会返回
                log.info("控制面板已关闭，管家退出")
            except Exception:  # noqa: BLE001
                log.exception("控制面板启动失败，转为纯托盘常驻")
                self._quit.wait()
        else:
            self._quit.wait()

        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:  # noqa: BLE001
            pass


def _shutdown_kernels(app) -> None:
    try:
        app.guard.stop_all()
        log.info("退出清理：已停止全部线路")
    except Exception as e:  # noqa: BLE001
        log.exception("退出清理失败：%s", e)


def main() -> None:
    paths.ensure_dirs()
    setup_logging(verbose="--verbose" in sys.argv)
    log.info("========== Chrome 代理管家启动 pid=%s root=%s ==========", os.getpid(), paths.ROOT)
    app = App()
    atexit.register(_shutdown_kernels, app)   # 兜底：异常退出也别留孤儿内核
    app.run()
    log.info("========== 管家退出 ==========")
