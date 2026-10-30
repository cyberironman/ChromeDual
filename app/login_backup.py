# -*- coding: utf-8 -*-
"""Chrome 登录态（Cookie）自动备份与恢复 —— **按线路强隔离**。

为什么要有这个东西
------------------
Chrome 127+ 在 Windows 上启用了应用绑定加密（App-Bound Encryption，ABE）：
Cookie 用 Local State 里的 app_bound_encrypted_key 加密，解密必须由 Chrome 自己的
提权服务完成。**一旦这把钥匙对不上**（大版本升级、资料目录被换过密钥、跨目录
复制过 Cookies），Chrome 不报错，而是**静默把解不开的 Cookie 全删掉**，
表现就是「所有网站同时退出登录」。本机 2026-10-29 就这么丢过一次。

★ 恢复时必须成对恢复 —— 只把 Cookies 拷回去、Local State 还是新的，
  照样解不开、照样被清。

★ 2026-10-30 修正的四个跨线路缺陷（上一版每个都会出事）：
  1. 备份目录名用 `{stamp}_{profile}`，四条线 profile 都叫 Default → 互相重名；
  2. 「最新指纹」扫全局 → 别的线路指纹相同就跳过本次备份（静默丢备份）；
  3. 裁剪按全局 12 份 → 活跃线路会把其他线路的历史挤掉；
  4. restore() 取「全局最新一份」→ **可能把 A 线路的 Cookie 灌进 B 线路**。
  现在备份根是 var\\backups\\<线路>\\，去重/裁剪/恢复全部只看本线路。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import psutil

from app import paths
from app.logging_setup import log

KEEP = 12         # 每条线路最多保留多少份

# (相对路径, 是否在 profile 子目录下)
ITEMS = [
    ("Network/Cookies", True),
    ("Login Data", True),
    ("Local State", False),
]


def _root(line_id: str) -> Path:
    return paths.backup_dir(line_id)


def _paths(udd: str | os.PathLike, profile: str = "Default"):
    root = Path(udd)
    prof = root / (profile or "Default")
    out = []
    for rel, in_prof in ITEMS:
        out.append((rel, (prof / rel) if in_prof else (root / rel)))
    return root, prof, out


def chrome_busy(udd: str | os.PathLike) -> int | None:
    """这套资料是否正在被 Chrome 使用（在跑就复制不了 Cookies —— 它是独占锁）。"""
    if not udd:
        return None
    target = str(udd).lower().replace("/", "\\").rstrip("\\")
    if not target:
        return None
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if "--type=" in cmd:
                continue
            if target in cmd.lower().replace("/", "\\"):
                return p.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def cookie_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.5)
        try:
            return int(db.execute("select count(*) from cookies").fetchone()[0])
        except sqlite3.Error:
            return None
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return None


def fingerprint(udd: str | os.PathLike, profile: str = "Default") -> str:
    """判断「内容有没有变」，变了才备份，避免刷一堆重复目录。"""
    root, prof, items = _paths(udd, profile)
    parts = []
    for rel, p in items:
        parts.append(f"{rel}:{p.stat().st_size}" if p.is_file() else f"{rel}:-")
    ck = dict(items).get("Network/Cookies")
    parts.append(f"n:{cookie_count(ck) if ck else '-'}")
    ls = root / "Local State"
    if ls.is_file():
        try:
            import hashlib
            oc = (json.load(open(ls, encoding="utf-8")).get("os_crypt") or {})
            # ⚠ 以前这里用内置 hash()：Python 3 对 str 的 hash 每个进程随机化，
            #   同一份 Local State 每次算出来都不一样 → 「内容没变就不备份」永远失效，
            #   而且跨快照根本没法比。改用 sha1（稳定、可跨进程比较）。
            def _h(v: str) -> str:
                return hashlib.sha1(str(v).encode("utf-8")).hexdigest()[:8]
            parts.append(f"k:{_h(oc.get('encrypted_key', ''))}"
                         f"/{_h(oc.get('app_bound_encrypted_key', ''))}")
        except Exception:  # noqa: BLE001
            pass
    return "|".join(parts)


def _latest_fp(line_id: str) -> str | None:
    """**只看本线路**最近一份的指纹。"""
    root = _root(line_id)
    if not root.is_dir():
        return None
    for d in sorted((x for x in root.iterdir() if x.is_dir()), reverse=True):
        f = d / "manifest.json"
        if f.is_file():
            try:
                return json.load(open(f, encoding="utf-8")).get("fingerprint")
            except Exception:  # noqa: BLE001
                return None
    return None


def snapshot(line_id: str, udd: str | os.PathLike, profile: str = "Default",
             tag: str = "", force: bool = False) -> Path | None:
    """成对备份 Cookies + 登录库 + Local State。返回备份目录，跳过时返回 None。"""
    if not line_id or not udd:
        return None
    pid = chrome_busy(udd)
    if pid:
        return None          # Chrome 独占着 Cookies，硬拷会失败；交给下次

    root, prof, items = _paths(udd, profile)
    if not (prof / "Network" / "Cookies").is_file():
        return None

    fp = fingerprint(udd, profile)
    if not force and fp == _latest_fp(line_id):
        return None

    dst_root = _root(line_id)
    dst_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = dst_root / stamp
    if dst.exists():
        dst = dst_root / f"{stamp}_{int(time.time() * 1000) % 1000}"
    dst.mkdir(parents=True, exist_ok=True)

    saved = []
    for rel, p in items:
        if not p.is_file():
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, out)
            saved.append({"rel": rel, "size": out.stat().st_size})
        except Exception as e:  # noqa: BLE001
            saved.append({"rel": rel, "error": str(e)})

    meta = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "line": line_id,
        "user_data_dir": str(root),
        "profile": profile,
        "tag": tag,
        "cookie_count": cookie_count(prof / "Network" / "Cookies"),
        "fingerprint": fp,
        "files": saved,
    }
    (dst / "manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    _prune(line_id)
    return dst


def _prune(line_id: str, keep: int = KEEP) -> None:
    """**只裁本线路**的备份。"""
    root = _root(line_id)
    if not root.is_dir():
        return
    dirs = sorted(x for x in root.iterdir() if x.is_dir())
    if len(dirs) <= keep:
        return
    for d in dirs[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def list_backups(line_id: str) -> list[dict]:
    root = _root(line_id)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(x for x in root.iterdir() if x.is_dir()):
        f = d / "manifest.json"
        if not f.is_file():
            continue
        try:
            m = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        m["path"] = str(d)
        m["line"] = m.get("line") or line_id
        out.append(m)
    return out


def restore(line_id: str, src_dir: str | Path | None = None,
            udd: str | os.PathLike | None = None,
            profile: str = "Default", assume_yes: bool = False) -> str:
    """把**本线路**的一份备份成对恢复回去。src_dir 为空则用本线路最近一份。

    ⚠ 不做「全局最新一份」兜底 —— 那是把 A 线路的 Cookie 灌进 B 线路的入口。
    """
    if udd is None:
        udd = paths.chrome_dir(line_id)
    root_dir = _root(line_id)

    src = Path(src_dir) if src_dir else None
    if src is None:
        bs = list_backups(line_id)
        if not bs:
            return f"线路 {line_id} 没有任何备份可恢复"
        src = Path(bs[-1]["path"])
    if not src.is_dir():
        return f"备份目录不存在：{src}"
    # 跨线路硬拦：要恢复的目录必须在本线路的备份根下面
    try:
        src.resolve().relative_to(root_dir.resolve())
    except (ValueError, OSError):
        return (f"拒绝恢复：{src} 不属于线路 {line_id} 的备份目录（{root_dir}）。"
                f"跨线路恢复会把别的账户的登录态灌进来。")

    # ★ 破坏性门禁：检查窗口是否在跑 → 先做完整快照 → 交互确认（或显式 --yes）。
    #   恢复是**覆盖**动作，覆盖错了就再也回不来，所以必须走 safety.guard。
    from app import safety
    try:
        safety.guard("恢复登录态", line_id=line_id, udd=udd, profile=profile,
                     assume_yes=assume_yes,
                     what="覆盖 Cookies / Login Data / Local State（三件套成对覆盖）")
    except safety.Refused as e:
        log.warning("[%s] 恢复被拦下：%s", line_id, e)
        return str(e)

    root, prof, _ = _paths(udd, profile)
    done = []
    for rel, in_prof in ITEMS:
        s = src / rel
        if not s.is_file():
            continue
        d = (prof / rel) if in_prof else (root / rel)
        d.parent.mkdir(parents=True, exist_ok=True)
        for extra in (d.parent / (d.name + "-journal"), d.parent / (d.name + "-wal"),
                      d.parent / (d.name + "-shm")):
            if extra.exists():
                try:
                    extra.unlink()
                except Exception:  # noqa: BLE001
                    pass
        shutil.copy2(s, d)
        done.append(f"{rel} ({d.stat().st_size} bytes)")
    log.info("[%s] 登录态已从 %s 恢复", line_id, src)
    return "已恢复：" + "，".join(done) if done else "备份里没有可恢复的文件"
