# -*- coding: utf-8 -*-
"""PAC 静态服务（直连线路用 --proxy-pac-url 取分流脚本）。"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from app.logging_setup import log
from app.net import port_open

# PAC 里出现的 127.0.0.1:<端口>
_PORT_RE = re.compile(r"127\.0\.0\.1:(\d{2,5})")


def render(src_dir: Path, dst_dir: Path, slots: dict[int, int],
           tokens: dict[str, str] | None = None) -> Path:
    """把 PAC 模板渲染成运行期脚本。

    为什么必须渲染：端口改为随机分配以后，模板里再也写不死数字。
    这里做两件事：
      1. 替换 `__PORT_VIDEO__` 这类占位符；
      2. 兜底替换老版本遗留的字面端口（10888 / 10898 / 10890），
         免得改了模板忘了改脚本，PAC 指向一个没人听的端口。
    渲染结果放 var/runtime/pac/，assets/ 里的模板保持原样（可进版本库）。
    """
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    tokens = tokens or {}
    n = 0
    for f in sorted(src_dir.glob("*.pac")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("PAC 模板读取失败 %s：%s", f, e)
            continue
        for k, v in tokens.items():
            text = text.replace(k, str(v))
        left = re.findall(r"__PORT_[A-Z_]+__", text)

        def _sub(m: re.Match) -> str:
            old = int(m.group(1))
            return f"127.0.0.1:{slots.get(old, old)}"

        text = _PORT_RE.sub(_sub, text)
        try:
            (dst_dir / f.name).write_text(text, encoding="utf-8")
            n += 1
        except OSError as e:
            log.warning("PAC 渲染写入失败 %s：%s", f, e)
        if left:
            log.warning("PAC %s 还有未替换的占位符：%s", f.name, ", ".join(sorted(set(left))))
    log.info("PAC 已按当前端口渲染：%s（%d 个文件）", dst_dir, n)
    return dst_dir


class PacServer:
    """在后台线程里提供 pac 目录的静态 HTTP 服务。"""

    def __init__(self, directory: Path, port: int):
        self.directory = Path(directory)
        self.port = int(port)
        self._srv: Any = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if port_open(self.port):
            log.info("PAC 端口 %s 已有服务，直接复用", self.port)
            return True
        if not self.directory.is_dir():
            log.error("PAC 目录不存在：%s", self.directory)
            return False
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class _PacHandler(SimpleHTTPRequestHandler):
            """pythonw.exe 下 sys.stderr 为 None，父类 log_message 会抛异常，
            外部表现就是「连上但立刻断开、空回复」（Chrome 报 PAC 取不到）。"""

            def log_message(self, fmt, *args):  # noqa: A003
                log.debug("PAC %s - %s", self.address_string(), fmt % args)

            def log_error(self, fmt, *args):
                log.warning("PAC 错误：%s", fmt % args)

        handler = functools.partial(_PacHandler, directory=str(self.directory))
        try:
            self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        except OSError as e:
            log.error("PAC 服务启动失败：%s", e)
            return False
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        name="pac-server", daemon=True)
        self._thread.start()
        log.info("PAC 服务已启动 127.0.0.1:%s -> %s", self.port, self.directory)
        return True

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        self._thread = None
        log.info("PAC 服务已停止")
