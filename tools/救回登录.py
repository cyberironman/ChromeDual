# -*- coding: utf-8 -*-
"""把「谷歌登录 Cookie」从历史快照里找回来（合并式，不覆盖你现在的数据）。

为什么需要它
------------
有些站会因为「资料目录被搬动」或「Chrome 大版本升级」而让 Chrome **静默删掉**
一批 Cookie —— 其中就包括谷歌登录 Cookie（SID 家族），表现是「主账户登出」。

好消息：只要 Local State 里那两个密钥（`encrypted_key` /
`app_bound_encrypted_key`）没变、资料目录路径也没变，快照里的那些 v20 密文
现在仍然解得开 —— 把它们放回 Cookie 库，登录就能回来，不用重新输密码。

本工具做什么
------------
1. 先确认该线路的 Chrome 已经关干净（开着就写不进去，写了也会被覆盖）；
2. 过 `safety.guard`：完整快照 + 审计日志（脚本要加 --yes）；
3. 在所有快照里挑「谷歌登录 Cookie 最多」的那一份；
4. **只把谷歌登录类 Cookie 合并进去**（同 host+name 覆盖，其余一律不动），
   顺带把 `__Host-GAPS` 这类账号选择器带上；
5. 打印前后对比，并提示「重启 Chrome 看效果」。

用法
----
    python tools\\救回登录.py                 # 先看有什么，不动手
    python tools\\救回登录.py --apply --yes   # 真合并（需先关掉该线路的 Chrome）
    python tools\\救回登录.py --line profile-a --apply --yes
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import login_guard, paths, safety  # noqa: E402
from app.config_store import load_profiles  # noqa: E402
from app.logging_setup import log, setup_logging  # noqa: E402
from app.procs import chrome_running_with  # noqa: E402


def _lines() -> dict[str, Path]:
    """线路 id → 资料目录。

    优先取线路自己配的 `chrome.user_data_dir`，没配就用 paths.chrome_dir() 的
    默认约定 —— 顺序必须和 app/line.py 的 Line.user_data_dir 完全一致，
    否则这里算出来的路径和真正在用的那份不是同一个。
    """
    raw = load_profiles()
    out = {}
    for item in (raw.get("lines") or []):
        lid = item.get("id") or ""
        if not lid:
            continue
        cfg = item.get("chrome") or {}
        raw_dir = str(cfg.get("user_data_dir") or "").strip()
        p = Path(raw_dir) if raw_dir else paths.chrome_dir(lid)
        out[lid] = p if p.is_absolute() else (paths.ROOT / p)
    return out


def _cookie_table(src: Path) -> tuple[list[str], list[dict]]:
    """读一份 Cookie 库：返回 (列名, 行字典列表)。"""
    tmp = Path(tempfile.gettempdir()) / f"rg_{src.parent.parent.name}_{src.stat().st_mtime_ns}.db"
    shutil.copy2(src, tmp)
    try:
        con = sqlite3.connect(str(tmp))
        con.row_factory = sqlite3.Row
        cols = [r[1] for r in con.execute("PRAGMA table_info(cookies)").fetchall()]
        rows = [dict(r) for r in con.execute("select * from cookies").fetchall()]
        con.close()
        return cols, rows
    finally:
        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass


def _is_login_cookie(host: str, name: str) -> bool:
    return (host in login_guard.GOOGLE_HOSTS or host.endswith(".google.com")
            or host.endswith(".youtube.com")) and name in login_guard.SID_NAMES


def _report(rows: list[dict]) -> tuple[int, int]:
    login = [r for r in rows if _is_login_cookie(r["host_key"], r["name"])]
    v20 = sum(1 for r in login
              if bytes(r.get("encrypted_value") or b"")[:2] == b"v2")
    return len(login), v20


def main() -> int:
    ap = argparse.ArgumentParser(description="从历史快照找回谷歌登录 Cookie")
    ap.add_argument("--line", default="", help="线路 id（默认用 profiles.json 里的第一条）")
    ap.add_argument("--apply", action="store_true", help="真的写回（不加就只看）")
    ap.add_argument("--yes", action="store_true", help="非交互确认（脚本必加）")
    args = ap.parse_args()

    setup_logging()
    lid = args.line or next(iter(_lines()), "")
    dirs = _lines()
    if lid not in dirs:
        print(f"没有这条线路：{lid}（可选：{', '.join(dirs)}）")
        return 2
    udd = dirs[lid]
    prof = udd / "Default"
    live = prof / "Network" / "Cookies"
    if not live.is_file():
        print(f"当前 Cookie 库不存在：{live}")
        return 2

    print(f"线路：{lid}   资料目录：{udd}")
    pid = chrome_running_with(udd)
    if pid:
        print(f"\n✗ 该资料目录的 Chrome 还开着（PID={pid}）。")
        print("  Cookie 库被独占锁住，现在写不进去。请先关掉那个窗口再运行本工具。")
        return 3

    # —— 找最佳快照
    best = login_guard.best_snapshot_for_google(lid)
    if not best:
        print("\n没有可用快照（var\\backups 下没有带 Cookies 的目录）。")
        return 4
    snap_dir, info = best
    snap_ck = snap_dir / "Network" / "Cookies"
    print(f"最佳快照：{snap_dir.name}  谷歌登录 Cookie {info['sid']} 条"
          f"（其中 v20 {info['v20']} 条）· 总 Cookie {info['total']}")

    _cols, snap_rows = _cookie_table(snap_ck)
    want = [r for r in snap_rows if _is_login_cookie(r["host_key"], r["name"])]
    if not want:
        print("\n这份快照里也没有谷歌登录 Cookie，换一条线路或另找时间点吧。")
        return 4

    _cols2, live_rows = _cookie_table(live)
    cur_login, _cur_v20 = _report(live_rows)
    print(f"当前：谷歌登录 Cookie {cur_login} 条 · 总 Cookie {len(live_rows)}")

    # —— 密钥一致性（决定 v20 能不能解开）
    snap_ls = snap_dir / "Local State"
    cur_ls = udd / "Local State"
    if snap_ls.is_file() and cur_ls.is_file():
        try:
            import hashlib

            def _k(p: Path) -> dict:
                d = json.loads(p.read_text(encoding="utf-8")).get("os_crypt") or {}
                return {k: hashlib.sha1(str(v).encode()).hexdigest()[:12]
                        for k, v in d.items() if isinstance(v, str)}
            a, b = _k(snap_ls), _k(cur_ls)
            same = all(a.get(k) == b.get(k) for k in ("encrypted_key", "app_bound_encrypted_key"))
            print(f"密钥比对：{'一致 ✓（v20 应该能解开）' if same else '不一致 ✗（v20 可能仍解不开，只能救 v10）'}")
            if not same:
                print("  ABE 密钥变过 —— 那批 v20 密文已作废，别指望全回来。")
            # 成对原则：密钥不同就必须连 Local State 一起还原，否则白弄
        except Exception as e:  # noqa: BLE001
            print(f"密钥比对跳过（{e}）")

    n_v20 = sum(1 for r in want if bytes(r.get("encrypted_value") or b"")[:2] == b"v2")
    print(f"准备合并 {len(want)} 条（v20 {n_v20} 条）")

    if not args.apply:
        print("\n（这是预览。确认无误后加 --apply --yes 再跑一次。）")
        for r in want[:12]:
            mark = "v20" if bytes(r.get("encrypted_value") or b"")[:2] == b"v2" else "v10"
            print(f"   {r['host_key']:<34} {r['name']:<22} {mark}")
        return 0

    # —— 过门禁：完整快照 + 审计（失败就中止）
    try:
        bak = safety.guard(f"救回谷歌登录（{snap_dir.name}）", line_id=lid, udd=udd,
                           profile="Default", assume_yes=args.yes,
                           what=f"把 {len(want)} 条谷歌登录 Cookie 合并进当前 Cookie 库")
    except safety.Refused as e:
        print(f"\n✗ {e}")
        return 5
    print(f"改前快照：{bak}")

    # —— 真合并
    cols = [c for c in _cols2 if c in want[0]]
    placeholders = ",".join("?" for _ in cols)
    upd = ",".join(f"{c}=excluded.{c}" for c in cols
                   if c not in ("host_key", "name", "path"))
    sql = (f"insert into cookies ({','.join(cols)}) values ({placeholders}) "
           f"on conflict(host_key,name,path) do update set {upd}")

    con = sqlite3.connect(str(live))
    done = 0
    for r in want:
        try:
            con.execute(sql, [r.get(c) for c in cols])
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"   跳过 {r['host_key']} {r['name']}：{e}")
    con.commit()
    con.close()

    after, _ = _report(_cookie_table(live)[1])
    safety._audit(f"救回谷歌登录 {lid}：合并 {done} 条（{cur_login} → {after}）"
                  f" 来源 {snap_dir.name}")
    print(f"\n✓ 合并完成：谷歌登录 Cookie {cur_login} → {after} 条（写入 {done} 条）")
    print("  现在重新打开这条线路的 Chrome，应该就是已登录状态。")
    print("  如果仍显示未登录：说明那批密文已被 ABE 作废，只能手动重登一次。")
    log.info("[救回登录] %s 合并 %s 条，%s → %s", lid, done, cur_login, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
