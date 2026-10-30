# -*- coding: utf-8 -*-
"""控制面板窗口（pywebview + Edge WebView2）。

为什么是 pywebview 而不是 tkinter：
    本机这套 Python 3.13 没编 tkinter（`import tkinter` 直接 ModuleNotFoundError），
    而 WebView2 运行时是系统自带的，pywebview 直接能用。
    窗口是真正的 Windows 窗口：有标题栏、能最小化、任务栏里有独立条目。

窗口生命周期：
    * 启动时创建为隐藏（hidden=True），点头图标才 show()
    * 点窗口的 × 只 hide()，进程和托盘继续工作
    * 退出管家时由 act_quit() 调 destroy()，webview.start() 才会返回
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path

from app import paths
from app.logging_setup import log

HTML_FILE = paths.APP / "panel.html"
TITLE = "Chrome 代理管家"

_USER32 = ctypes.windll.user32
_SW_HIDE = 0
_SW_SHOW = 5
_SW_RESTORE = 9

# --- Win32 图标相关常量 --------------------------------------------------------
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x10
_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1

# 64 位下句柄是 8 字节，不声明 argtypes 会被截断成 32 位（图标就设不上）
_USER32.LoadImageW.restype = ctypes.c_void_p
_USER32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_USER32.SendMessageW.restype = ctypes.c_void_p
_USER32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p]


def _icon_file() -> Path:
    return paths.ICON if paths.ICON.is_file() else paths.ICON_ALT


def apply_icon(hwnd: int) -> bool:
    """把窗口的标题栏小图标 + 任务栏大图标换成自家 ico。

    pywebview 的 create_window() 没有 icon 参数，窗口默认继承 pythonw.exe 的
    Python 图标。只能拿到 hwnd 之后用 WM_SETICON 硬设。
    """
    ico = _icon_file()
    if not hwnd or not ico.is_file():
        return False
    ok = False
    for px, which in ((32, _ICON_BIG), (16, _ICON_SMALL)):
        hicon = _USER32.LoadImageW(None, str(ico), _IMAGE_ICON, px, px, _LR_LOADFROMFILE)
        if hicon:
            _USER32.SendMessageW(ctypes.c_void_p(hwnd), _WM_SETICON,
                                 ctypes.c_void_p(which), ctypes.c_void_p(hicon))
            ok = True
    return ok


def _force_foreground(hwnd: int) -> None:
    """把窗口真正提到前台。

    后台进程调 SetForegroundWindow 常被系统静默拒绝，
    标准解法是 AttachThreadInput 把自己的输入队列临时挂到当前前台线程上。
    """
    k32 = ctypes.windll.kernel32
    fg = _USER32.GetForegroundWindow()
    tid_fg = wintypes.DWORD()
    _USER32.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(tid_fg))
    tid_me = k32.GetCurrentThreadId()
    attached = False
    try:
        if tid_fg.value and tid_fg.value != tid_me:
            attached = bool(_USER32.AttachThreadInput(
                ctypes.c_void_p(tid_me), ctypes.c_void_p(tid_fg.value), True))
        _USER32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        _USER32.BringWindowToTop(ctypes.c_void_p(hwnd))
        _USER32.SetFocus(ctypes.c_void_p(hwnd))
    except Exception:  # noqa: BLE001
        log.debug("强制前台失败", exc_info=True)
    finally:
        if attached:
            try:
                _USER32.AttachThreadInput(
                    ctypes.c_void_p(tid_me), ctypes.c_void_p(tid_fg.value), False)
            except Exception:  # noqa: BLE001
                pass


def _fit_on_screen(hwnd: int) -> None:
    """窗口比工作区高时往下挪回来（900px 高在 1080 屏上会露出屏幕外）。"""
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    work = _RECT()
    if not _USER32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):  # SPI_GETWORKAREA
        return
    r = wintypes.RECT()
    _USER32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    x = min(max(r.left, work.left), max(work.left, work.right - w))
    y = min(max(r.top, work.top), max(work.top, work.bottom - h))
    if (x, y) != (r.left, r.top):
        _USER32.SetWindowPos(ctypes.c_void_p(hwnd), None, x, y, 0, 0,
                             0x0001 | 0x0004)   # NOSIZE | NOZORDER
        log.info("窗口超出工作区，已挪到 (%s,%s)", x, y)


class Api:
    """页面 JS 能调到的接口：window.pywebview.api.status() / .act(name, arg)

    ⚠ 属性名**必须是下划线开头**。
    pywebview 6.x 的 util.get_functions() 会递归遍历 js_api 对象的
    **所有公开属性**（`name.startswith('_')` 才跳过），只要某个公开属性是
    非可调用对象、且带 __module__，就继续往里钻。原来叫 self.app，
    于是钻进 App → panel → window → .native(.NET Form) → AccessibilityObject
    → Bounds.Empty.Empty.Empty…，每层都返回新对象，靠 id() 去重拦不住，
    最终 maximum recursion depth exceeded，主线程卡死在 create_window 里：
    托盘图标点了没反应、控制口 ping 不回、面板永远弹不出来。

    所以这里只留私有引用，公开成员只有两个普通方法（函数是叶子，不再递归）。
    """

    __slots__ = ("_app",)

    def __init__(self, app) -> None:
        self._app = app

    def status(self) -> dict:
        try:
            data = self._app.panel_status()
            log.debug("面板拉状态：%s", data.get("text"))
            return data
        except Exception as e:  # noqa: BLE001
            log.exception("面板读取状态失败：%s", e)
            return {"ok": False, "level": "bad", "text": "状态读取失败",
                    "lines": [], "autostart": False, "busy": False}

    def act(self, name: str, arg=None) -> dict:
        try:
            return self._app.panel_action(name, arg)
        except Exception as e:  # noqa: BLE001
            log.exception("面板操作失败：%s/%s", name, e)
            return {"ok": False, "msg": f"失败：{e}"}


class Panel:
    # pywebview 遍历 js_api 属性图时的哨兵：带这个标记的对象一律不往里钻
    _serializable = False

    def __init__(self, app) -> None:
        self.app = app
        self.window = None
        self.ready = threading.Event()

    # -- 创建 ---------------------------------------------------------------
    def create(self) -> bool:
        try:
            import webview
        except Exception as e:  # noqa: BLE001
            log.error("没装 pywebview，控制面板不可用：%s", e)
            return False
        if not HTML_FILE.is_file():
            log.error("找不到面板页面：%s", HTML_FILE)
            return False
        try:
            html = HTML_FILE.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.error("读面板页面失败：%s", e)
            return False

        try:
            # 用 html 字符串而不是 file:// —— WebView2 下 file:// 与 js_api 注入
            # 偶尔会因为源不同互相看不见，内嵌最稳（页面不到 12KB）。
            self.window = webview.create_window(
                TITLE, html=html, js_api=Api(self.app),
                width=720, height=940, x=140, y=30,
                min_size=(600, 560), hidden=True,
                background_color="#F3F4F7", text_select=False,
            )
            self.window.events.closing += self._on_closing
            log.info("控制面板已创建（隐藏状态）")
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("创建控制面板失败：%s", e)
            self.window = None
            return False

    # -- Win32 兜底（pywebview 的 show/hide 偶尔会卡在 GUI 队列上）-------------
    @staticmethod
    def _find_hwnd() -> int | None:
        """只在**本进程**的窗口里找。别的程序也可能叫「Chrome 代理管家」。"""
        me = os.getpid()
        found: list[int] = []
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):  # noqa: ANN001
            pid = wintypes.DWORD()
            _USER32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value != me:
                return True
            n = _USER32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _USER32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == TITLE:
                    found.append(hwnd)
                    return False
            return True

        _USER32.EnumWindows(proc_type(_cb), 0)
        return found[0] if found else None

    # -- 事件 ---------------------------------------------------------------
    def _on_closing(self) -> bool:
        """点 × 只隐藏。返回 False 阻止窗口真的被关掉。"""
        try:
            hwnd = self._find_hwnd()
            if hwnd:
                _USER32.ShowWindow(hwnd, _SW_HIDE)
            else:
                self.window.hide()
            log.info("控制面板已隐藏（管家仍在托盘运行）")
        except Exception:  # noqa: BLE001
            log.exception("隐藏面板失败")
        return False

    def _on_started(self) -> None:
        self.ready.set()

        def _late() -> None:
            for _ in range(8):
                hwnd = self._find_hwnd()
                if hwnd and apply_icon(hwnd):
                    log.info("面板图标已换成本家 ico（hwnd=%s）", hwnd)
                    return
                threading.Event().wait(0.4)
            log.warning("面板图标设置失败（没找到窗口或 ico 缺失）")

        threading.Thread(target=_late, daemon=True).start()

    # -- 主循环（阻塞，必须在主线程调用）------------------------------------
    def start(self) -> None:
        import webview
        webview.start(self._on_started)

    # -- 显示 ---------------------------------------------------------------
    def show(self) -> bool:
        w = self.window
        if w is None:
            return False

        # pywebview 的 show() 要走它自己的 GUI 队列，实测有卡死的情况。
        # 所以：限时调它，然后一律用 Win32 的 ShowWindow 把窗口顶出来。
        state = {"done": False}

        def call() -> None:
            try:
                w.show()
            except Exception:  # noqa: BLE001
                log.exception("pywebview show 异常")
            finally:
                state["done"] = True

        threading.Thread(target=call, daemon=True).start()

        waited = 0.0
        hwnd = None
        while waited < 4.0:
            hwnd = self._find_hwnd()
            if hwnd or state["done"]:
                break
            threading.Event().wait(0.15)
            waited += 0.15

        if not hwnd:
            hwnd = self._find_hwnd()
        if hwnd:
            try:
                apply_icon(hwnd)          # 每次显示都重设一次，防被窗体重绘覆盖
                _USER32.ShowWindow(ctypes.c_void_p(hwnd), _SW_RESTORE)
                _USER32.ShowWindow(ctypes.c_void_p(hwnd), _SW_SHOW)
                _fit_on_screen(hwnd)
                _force_foreground(hwnd)
                # 兜底：AttachThreadInput 也被拒时（前台是最大化 Word/浏览器），
                # 置顶 2 秒强行压上去，再恢复普通层级。
                if _USER32.GetForegroundWindow() != hwnd:
                    _USER32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1),
                                         0, 0, 0, 0, 0x0001 | 0x0002)

                    def _untop() -> None:
                        try:
                            _USER32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-2),
                                                 0, 0, 0, 0, 0x0001 | 0x0002)
                        except Exception:  # noqa: BLE001
                            pass

                    threading.Timer(2.0, _untop).start()
                log.info("控制面板已显示（hwnd=%s，pywebview=%s）", hwnd, state["done"])
                return True
            except Exception:  # noqa: BLE001
                log.exception("ShowWindow 失败")
        log.warning("没找到控制面板窗口句柄（pywebview done=%s）", state["done"])
        return bool(state["done"])

    def destroy(self) -> None:
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:  # noqa: BLE001
            log.debug("销毁面板失败", exc_info=True)
