# -*- coding: utf-8 -*-
"""端口随机分配 —— **全项目唯一知道「用哪个端口」的地方**。

为什么要有这个模块
------------------
以前端口是写死的：主线路 10888/10889、第二线路 10898/10899、第三线路 10948、
PAC 10890、控制口 10895。三个代理软件（v2rayN / Codex 管家 / 本程序）全挤在
10800~10999 这一小段里，谁先绑谁活。绑不上的那个表现是「内核掉线」「PAC 取不到」，
日志里看不出是端口冲突，查一次要几个钟头（2026-10-29 那次事故）。

现行规则（用户 2026-10-30 明确要求）
-----------------------------------
1. 每个「端口槽位」独立随机，范围 **20000 ~ 59999**，互不重复；
   槽位名：`ctl`（单实例锁+控制口）、`pac`（PAC 静态服务）、`line:<线路 id>`（内核入口）。
2. 挑出来的端口写 `var/run/ports.json` **持久化** —— 重启沿用、不跳变。
   端口一跳，已经开着的 Chrome 窗口就指向旧端口，等于断网。
3. 每次启动复核：持久化的端口若被别的程序占用（或不在随机段里）→ 重新随机并写回。
4. 老的 10800~10999 固定端口**一律淘汰**，不参与「沿用」，保证一次切换干净。
5. 内核需要 http 与 socks 两个相邻端口时，用 `paired=("line:x",)` 声明，
   保证 p 和 p+1 都空闲（socks 端口 = http 端口 + 1，沿用原设计）。
"""
from __future__ import annotations

import json
import os
import random
import socket
import time
from typing import Callable, Iterable

from app import paths
from app.logging_setup import log

RANGE_START = 20000
RANGE_END = 39999

# 本机三个兄弟程序各占一段，互不重叠（同时随机也撞不到一起）：
#   分应用代理管家 AppProxy  15002–19998
#   本程序（分资料代理）      20000–39998
#   分应用代理 Codex 管家     40000–49148
# 三段全部落在「动态段之上、49152 之下」的安全区。

# 老的固定端口段：这些值即使系统空闲也不再沿用（避免「又回到 888」）
LEGACY_LO, LEGACY_HI = 10800, 10999

# 无论区间怎么调，这些段一律不能落进去（2026-10-30 补）：
#   * 1–1023      要管理员权限才能绑
#   * 1024–15000  本机动态端口段（netsh 实测：启动 1024、数量 13977）——
#                 出站连接会从这一段随机拿本地端口，listen 落进去迟早 EADDRINUSE
#   * 49152–65535 Windows 默认动态段位置，保守整段避开
DANGER_RANGES = ((1, 1023), (1024, 15000), (49152, 65535))

STATE = paths.RUN / "ports.json"


# ------------------------------------------------------------------ 基础判断
def can_bind(port: int) -> bool:
    """127.0.0.1:port 现在能不能绑上（= 没有别的程序在听）。"""
    if not (1 <= int(port) <= 65535):
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Windows 上 SO_REUSEADDR 会让「已被监听」也绑成功，这里**绝对不能**设
    try:
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _in_range(port: int) -> bool:
    p = int(port)
    if any(lo <= p <= hi for lo, hi in DANGER_RANGES):
        return False
    return RANGE_START <= p <= RANGE_END


def _legacy(port: int) -> bool:
    return LEGACY_LO <= int(port) <= LEGACY_HI


# ------------------------------------------------------------------ 持久化
def load_state() -> dict[str, int]:
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        if isinstance(v, (int, float, str)):
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def save_state(state: dict[str, int]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        payload = dict(state)
        payload["_updated"] = int(time.time())
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE)          # 原子替换，避免半截文件
    except Exception as e:  # noqa: BLE001
        log.warning("端口表写入失败（本次仍可用，只是重启会重挑）：%s", e)


def get(slot: str, default: int = 0) -> int:
    """读某个槽位已分配的端口（没分配过返回 default）。"""
    try:
        return int(load_state().get(slot) or default)
    except Exception:  # noqa: BLE001
        return default


def put(slot: str, port: int) -> None:
    st = load_state()
    st[slot] = int(port)
    save_state(st)


# ------------------------------------------------------------------ 挑选
def _pick(reserved: set[int], need_pair: bool, tries: int = 400) -> int:
    """在随机段里挑一个（或一对）空闲端口。挑不到就抛 RuntimeError。"""
    span = RANGE_END - RANGE_START - 1
    for _ in range(tries):
        p = RANGE_START + random.randrange(0, span)
        if p in reserved or (p + 1) in reserved:
            continue
        if not can_bind(p):
            continue
        if need_pair and not can_bind(p + 1):
            continue
        return p
    # 兜底：顺序扫一遍（极端情况，比如大量端口被占）
    for p in range(RANGE_START, RANGE_END - 1):
        if p in reserved or (p + 1) in reserved:
            continue
        if can_bind(p) and (not need_pair or can_bind(p + 1)):
            return p
    raise RuntimeError("找不到可用端口（20000-59999 全被占用？）")


def assign(wanted: dict[str, int], ours: Callable[[int], bool] | None = None,
           paired: Iterable[str] = ()) -> dict[str, int]:
    """给一组槽位分端口，返回 {slot: port}，并把结果写进 ports.json。

    wanted : {槽位名: 配置里写的旧端口}（0 表示「随你挑」）
    ours   : 判断「这个端口虽然有人在听，但那是本程序自己的内核」——
             重启管家时自家遗留内核还占着端口，不能当成冲突把它踢掉。
    paired : 需要 p 与 p+1 同时可用的槽位名集合（内核 http+socks）。
    """
    paired = set(paired)
    state = load_state()
    reserved: set[int] = set()
    out: dict[str, int] = {}
    changed: list[str] = []

    for slot in sorted(wanted):
        old = int(state.get(slot) or 0) or int(wanted.get(slot) or 0)
        keep = False
        if _in_range(old) and not _legacy(old) and old not in reserved:
            if can_bind(old) or (ours is not None and ours(old)):
                keep = True
        if keep:
            out[slot] = old
            reserved.add(old)
            reserved.add(old + 1)
            continue
        p = _pick(reserved, need_pair=slot in paired)
        out[slot] = p
        reserved.add(p)
        reserved.add(p + 1)
        changed.append(slot)

    merged = dict(state)
    merged.update(out)
    for k in list(merged):
        if k.startswith("_"):
            merged.pop(k)
    save_state(merged)

    if changed:
        log.info("端口随机分配：%s", "、".join(f"{s}={out[s]}" for s in changed))
    else:
        log.info("端口沿用上次分配：%s", "、".join(f"{s}={out[s]}" for s in sorted(out)))
    return out


def release_legacy_slots(prefix: str, keep: Iterable[str]) -> None:
    """把 prefix 开头、但已不在线路列表里的槽位清掉（删了线路不留垃圾）。"""
    keep = set(keep)
    st = load_state()
    dead = [k for k in st if k.startswith(prefix) and k not in keep]
    if not dead:
        return
    for k in dead:
        st.pop(k, None)
    save_state(st)
    log.info("清理已删除线路的端口槽位：%s", ", ".join(dead))
