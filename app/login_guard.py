# -*- coding: utf-8 -*-
"""登录态守门员 —— 让「改程序」在物理上碰不到「资料」。

为什么单独一个模块
------------------
2026-10-30 用户的意见，原话：
    「每次稍微改动一点 就会影响各个 chrome 的登录以及资料
      既然改的是程序本身 却总是去影响资料 非常不合理」

复盘那两次掉登录，机制其实只有三种，全都跟「程序代码逻辑」无关：
    ① **资料目录被复制/改名/换路径** —— Chrome 127+ 的 v20 Cookie 用 ABE
       （App-Bound Encryption，应用绑定加密）加密，密文里绑死了 user-data-dir
       的绝对路径；路径一变，Chrome 解不开就把那些 Cookie **静默删掉**，不报错。
    ② **Chrome 大版本升级** —— ABE 密钥轮换，旧的 v20 同样解不开。
    ③ **拿别的资料目录去「恢复」** —— 跨线路写 Cookies 和 Local State 不成对。

所以本模块只做两件事，都是**只读**的：
    * 冻结路径：每个线路的 --user-data-dir 第一次见到就记进
      var\\state\\frozen.json，以后对不上就告警（并拒绝自动重建目录）。
    * 记登录指纹：每次开窗前数一遍 Cookie（总数 / v20 数 / 谷歌登录 Cookie 数），
      和上次比。**变少就说明「在开窗之前就已经少了」**，能立刻把锅定位到
      「不是这次改代码造成的」，而不是又一轮瞎猜。

铁律：本模块**绝不往 data\\chrome 下写任何东西**（连临时文件都不放）。
      它只读 Cookie 库、只写 var\\ 下的两个 json 和一份日志。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from app import paths
from app.logging_setup import log

STATE_DIR = paths.VAR / "state"
FROZEN = STATE_DIR / "frozen.json"
LOGIN_STATE = STATE_DIR / "login_state.json"
EVENT_LOG = paths.LOGS / "login-events.log"

# 这些域的 Cookie 就是「登录」本身 —— 掉一条就少一处已登录的谷歌服务。
GOOGLE_HOSTS = (".google.com", "accounts.google.com", ".youtube.com",
                "accounts.youtube.com", ".google.co.kr", ".gstatic.com")
# 谷歌登录态的核心 Cookie 名（SID 家族）。少一个基本等于「退出登录」。
SID_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Host-1PLSID", "__Host-3PLSID", "__Host-GAPS", "LSID",
}


# ------------------------------------------------------------------ 只读指纹
def _copy_out(src: Path) -> Path | None:
    """把被 Chrome 独占锁住的 Cookie 库复制到临时目录再读。

    Chrome 用 FILE_SHARE_NONE 打开，外部直接读会 WinError 32；
    这里不复用任何已有文件，读完即删。
    """
    if not src.is_file():
        return None
    tmp = Path(tempfile.gettempdir()) / f"cd_lg_{os.getpid()}_{int(time.time()*1000)}.db"
    try:
        import shutil
        shutil.copy2(src, tmp)
        return tmp
    except Exception:  # noqa: BLE001
        return None


def _read_cookies(db: Path) -> list[tuple[str, str, str]] | None:
    """返回 [(host, name, 加密版本 v10/v20/plain)]，读不到返回 None。"""
    tmp = _copy_out(db)
    if tmp is None:
        return None
    try:
        con = sqlite3.connect(str(tmp))
        rows = con.execute(
            "select host_key, name, hex(substr(encrypted_value,1,3)) from cookies"
        ).fetchall()
        con.close()
        out = []
        for host, name, mark in rows:
            mark = (mark or "").lower()
            ver = {"763130": "v10", "763230": "v20"}.get(mark, "plain" if mark == "" else "other")
            out.append((host or "", name or "", ver))
        return out
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass


def _keysha(path: Path) -> dict:
    """Local State 里两个密钥的稳定指纹（用 hashlib，跨进程一致）。"""
    res = {"key": "", "abekey": ""}
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return res
    oc = d.get("os_crypt") or {}
    import base64
    for src, dst in (("encrypted_key", "key"),
                     ("app_bound_encrypted_key", "abekey")):
        v = oc.get(src)
        if not v:
            continue
        try:
            raw = base64.b64decode(v)
            res[dst] = hashlib.sha1(raw).hexdigest()[:12]
        except Exception:  # noqa: BLE001
            pass
    return res


def fingerprint(udd: str | os.PathLike, profile: str = "Default") -> dict:
    """一条线路此刻的登录态指纹。**纯只读**，Chrome 开着也能算。

    返回：
        total / v10 / v20       Cookie 总数与加密版本分布
        google / sid            谷歌域 Cookie 数、登录 SID 数
        db                      Cookie 库字节数
        key / abekey            Local State 两个密钥的 sha1 前缀
        reading                 True 表示真读到了（False = 被锁或者库不在）
    """
    root = Path(udd)
    prof = root / (profile or "Default")
    db = prof / "Network" / "Cookies"
    fp = {"line_root": str(root), "profile": profile, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
          "total": 0, "v10": 0, "v20": 0, "google": 0, "sid": 0,
          "db": 0, "key": "", "abekey": "", "reading": False}
    try:
        fp["db"] = db.stat().st_size
    except Exception:  # noqa: BLE001
        pass

    rows = _read_cookies(db)
    if rows is not None:
        fp["reading"] = True
        fp["total"] = len(rows)
        for host, name, ver in rows:
            if ver == "v10":
                fp["v10"] += 1
            elif ver == "v20":
                fp["v20"] += 1
            if host in GOOGLE_HOSTS or host.endswith(".google.com"):
                fp["google"] += 1
                if name in SID_NAMES:
                    fp["sid"] += 1

    fp.update(_keysha(root / "Local State"))

    ld = prof / "Login Data"
    try:
        fp["login_data"] = ld.stat().st_size
    except Exception:  # noqa: BLE001
        fp["login_data"] = 0
    return fp


# ------------------------------------------------------------------ 路径冻结
def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("[登录守门] 写 %s 失败：%s", path.name, e)


def freeze(line_id: str, udd: str | os.PathLike) -> dict:
    """登记/校验一条线路的资料目录。返回值里 ok=False 就是「路径被改过」。

    为什么必须拦：v20 Cookie 绑死这个路径字符串。改了路径 = 那批 Cookie
    当场作废，用户表现就是「登录全部消失」。
    """
    cur = str(Path(udd).resolve())
    data = _load(FROZEN)
    old = data.get(line_id)

    if not old:
        data[line_id] = {"path": cur, "since": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save(FROZEN, data)
        return {"ok": True, "first": True, "path": cur}

    if old.get("path") == cur:
        return {"ok": True, "first": False, "path": cur}

    _event(f"路径被改 {line_id}：{old.get('path')} → {cur}")
    return {"ok": False, "first": False, "path": cur, "old_path": old.get("path")}


def frozen_path(line_id: str) -> str:
    return (_load(FROZEN).get(line_id) or {}).get("path", "")


# ------------------------------------------------------------------ 变化比对
def _event(text: str) -> None:
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pid={os.getpid()} {text}\n")
    except Exception:  # noqa: BLE001
        pass
    log.info("[登录守门] %s", text)


def before_open(line_id: str, udd: str | os.PathLike, profile: str = "Default",
                name: str = "") -> list[str]:
    """开窗前跑一遍。返回给用户看的告警列表（空=一切正常）。

    重点：**先读、再比、最后才让调用方开窗**。这样任何一次「开了窗口发现
    没登录」都能立刻说清楚是不是这次造成的 —— 指纹是在开窗之前取的。
    """
    warns: list[str] = []
    frz = freeze(line_id, udd)
    if not frz["ok"]:
        warns.append(
            f"资料目录被改过：以前是 {frz['old_path']}，现在指向 {frz['path']}。\n"
            f"Chrome 会把绑在原路径上的加密 Cookie 全部作废（表现=全部退出登录）。\n"
            f"请把目录改回去，或确认要重新登录再继续。")

    fp = fingerprint(udd, profile)
    if not fp["reading"]:
        # 读不到（正被 Chrome 锁着）不算异常，本轮不比对
        return warns

    store = _load(LOGIN_STATE)
    prev = store.get(line_id) or {}
    if prev and prev.get("reading"):
        lost = prev.get("sid", 0) - fp["sid"]
        gone20 = prev.get("v20", 0) - fp["v20"]
        if lost > 0:
            warns.append(
                f"谷歌登录 Cookie 少了 {lost} 条（{prev.get('sid')} → {fp['sid']}）。"
                f"如果这次打开发现没登录，原因在**打开之前**，不是这次开窗造成的。")
        if gone20 > 0:
            warns.append(
                f"v20 加密 Cookie 少了 {gone20} 条（{prev.get('v20')} → {fp['v20']}）。"
                f"v20 只在「资料目录被搬动」或「Chrome 大版本升级」时才会失效。")
        if prev.get("abekey") and fp["abekey"] and prev["abekey"] != fp["abekey"]:
            warns.append(
                f"ABE 密钥变了（{prev['abekey']} → {fp['abekey']}）—— 这是 Chrome 升级或"
                f"重建密钥，旧 v20 Cookie 已作废。")
        if prev.get("total", 0) - fp["total"] > 30:
            warns.append(
                f"Cookie 总数明显下降（{prev.get('total')} → {fp['total']}）。")

    if warns:
        _event(f"{name or line_id} 开窗前告警：" + " / ".join(w.replace("\n", " ") for w in warns))
    fp["line"] = line_id
    store[line_id] = fp
    _save(LOGIN_STATE, store)
    return warns


def record_after_open(line_id: str, udd: str | os.PathLike, profile: str = "Default") -> dict:
    """开窗之后不再比对，只刷新「基线」—— 免得下次把用户自己正常的登录变化算成丢失。"""
    fp = fingerprint(udd, profile)
    if fp["reading"]:
        store = _load(LOGIN_STATE)
        fp["line"] = line_id
        store[line_id] = fp
        _save(LOGIN_STATE, store)
    return fp


def last_state(line_id: str) -> dict:
    return (_load(LOGIN_STATE).get(line_id) or {})


def summary(line_id: str, udd: str | os.PathLike, profile: str = "Default") -> str:
    """给面板用的一句话，如「Cookie 175 · 谷歌登录 0 · v20 0」。"""
    fp = last_state(line_id)
    if not fp.get("reading"):
        fp = fingerprint(udd, profile)
    if not fp.get("reading"):
        return "登录态读不到（窗口开着时正常）"
    tag = "已登录" if fp.get("sid", 0) >= 5 else ("未登录" if fp.get("sid", 0) == 0 else "登录不全")
    return (f"Cookie {fp.get('total', 0)} · 谷歌登录 {fp.get('sid', 0)} 条"
            f"（{tag}）· v20 {fp.get('v20', 0)}")


# ------------------------------------------------------- 从备份里找回登录态
def best_snapshot_for_google(line_id: str) -> tuple[Path, dict] | None:
    """在所有快照里找「谷歌登录 Cookie 最多」的那一份。"""
    root = paths.backup_dir(line_id)
    if not root.is_dir():
        return None
    best = None
    for d in sorted((x for x in root.iterdir() if x.is_dir())):
        db = d / "Network" / "Cookies"
        rows = _read_cookies(db)
        if not rows:
            continue
        sid = sum(1 for h, n, _ in rows if (h in GOOGLE_HOSTS or h.endswith(".google.com"))
                  and n in SID_NAMES)
        v20 = sum(1 for _, _, v in rows if v == "v20")
        if best is None or sid > best[1]["sid"]:
            best = (d, {"sid": sid, "v20": v20, "total": len(rows)})
    return best
