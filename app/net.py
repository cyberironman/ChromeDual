# -*- coding: utf-8 -*-
"""网络探测：端口、监听者归属、带内容校验的站点探测。

单独成模块的原因：这些是最常被别的模块调用的纯函数
（Guard 的保活、Line 的状态、重点网址巡检都要用），
以前和 Line/Guard 挤在一个 1000 行的文件里，改一处要读全文。
"""
from __future__ import annotations

import socket
import time

import psutil

from app.logging_setup import log

_LISTENER_CACHE: dict[int, tuple[float, list[tuple[int, str]]]] = {}
_LISTENER_TTL = 1.5


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """端口是否在监听。

    timeout 由 0.5 提到 1.0：全机 socket 紧张时，0.5 秒连本地端口也会超时，
    会被误判成「内核掉了」然后杀进程重拉，反而把还能用的内核弄死。
    """
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def free_port(near: int = 0) -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", near or 0))
        return s.getsockname()[1]


def port_listeners(port: int) -> list[tuple[int, str]]:
    """返回监听该端口的 [(pid, 命令行)]。

    为什么要这个：**「端口能连上」不等于「我们的内核在跑」**。本机踩过：
    历史遗留的孤儿 xray、手起的 http.server 占着 10888/10890，只看端口通不通
    就把它们当成自家内核，面板四条线全绿，其实一半不是我们起的。
    """
    if not port:
        return []
    now = time.time()
    hit = _LISTENER_CACHE.get(port)
    if hit and now - hit[0] < _LISTENER_TTL:
        return hit[1]

    out: list[tuple[int, str]] = []
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError) as e:
        log.debug("读取端口归属失败：%s", e)
        conns = None      # 读不到就退回「端口探测」结论，宁可乐观也别全标红
    if conns is not None:
        for c in conns:
            if not c.laddr or c.laddr.port != port:
                continue
            if c.status != psutil.CONN_LISTEN:
                continue
            pid = c.pid or 0
            cmd = ""
            if pid:
                try:
                    cmd = " ".join(psutil.Process(pid).cmdline())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    cmd = ""
            out.append((pid, cmd))
    _LISTENER_CACHE[port] = (now, out)
    return out


def proc_label(pid: int) -> str:
    """把 pid 变成「xray.exe (41392)」这种一眼能认的标签。"""
    if not pid:
        return "未知进程"
    try:
        p = psutil.Process(pid)
        return f"{p.name()} ({pid})"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return f"PID {pid}"


def uptime_text(pid: int) -> str:
    """内核已经跑了多久 —— 让「刚起的」和「几小时前的遗留」一眼可见。"""
    try:
        p = psutil.Process(pid)
        sec = max(0, int(time.time() - p.create_time()))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"


def probe_proxy(http_port: int, url: str, timeout: float = 8.0) -> tuple[bool, int, float]:
    """通过代理请求 url，返回 (是否成功, 状态码, 耗时秒)。"""
    import requests
    proxies = {"http": f"http://127.0.0.1:{http_port}",
               "https": f"http://127.0.0.1:{http_port}"}
    t0 = time.time()
    try:
        r = requests.get(url, proxies=proxies, timeout=timeout, stream=True,
                         headers={"User-Agent": "Mozilla/5.0 ChromeProxyGuard"})
        r.close()
        return r.status_code in (200, 204), r.status_code, time.time() - t0
    except Exception as e:  # noqa: BLE001
        log.debug("探测 %s 失败：%s", url, e)
        return False, 0, time.time() - t0


def probe_target(http_port: int, url: str, min_bytes: int = 20000,
                 must_contain: str = "", timeout: float = 20.0,
                 attempts: int = 2) -> dict:
    """**带内容校验**的探测：只拿状态码不够。

    踩过的坑：有些站经某些节点会返回 `200 OK` 但响应体 0 字节
    （Cloudflare 边缘对该出口 IP 的空壳响应）。只看状态码会误判成「通」，
    用户点进去却是白屏。判定标准：状态码 200/204 且读到 >= min_bytes 字节
    且（可选）包含关键词。空壳响应会重试，最多 attempts 次。
    """
    import requests
    proxies = {"http": f"http://127.0.0.1:{http_port}",
               "https": f"http://127.0.0.1:{http_port}"}
    res = {"ok": False, "code": 0, "bytes": 0, "cost": 0.0, "why": "", "attempts": 0}
    t0 = time.time()
    for i in range(max(1, attempts)):
        res["attempts"] = i + 1
        res.update({"ok": False, "code": 0, "bytes": 0, "why": ""})
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout, stream=False,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                    "Chrome/155.0.0.0 Safari/537.36",
                                      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            body = r.content or b""
            res["code"] = r.status_code
            res["bytes"] = len(body)
            if r.status_code not in (200, 204):
                res["why"] = f"HTTP {r.status_code}"
            elif len(body) < min_bytes:
                res["why"] = f"空壳响应（{len(body)} 字节）"
            elif must_contain and must_contain.lower() not in body.decode("utf-8", "ignore").lower():
                res["why"] = f"内容不含「{must_contain}」"
            else:
                res["ok"] = True
        except Exception as e:  # noqa: BLE001
            res["why"] = type(e).__name__
            log.debug("目标探测 %s 失败：%s", url, e)
        if res["ok"]:
            break
    res["cost"] = round(time.time() - t0, 2)
    return res
