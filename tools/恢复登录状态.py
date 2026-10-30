# -*- coding: utf-8 -*-
"""按线路恢复 Chrome 登录态（Cookies + Login Data + Local State 成对）。

用法：
    python 恢复登录状态.py                     列出四条线路各有几份备份
    python 恢复登录状态.py -l love             只看 love 这条线
    python 恢复登录状态.py -l love -n 2        恢复 love 第 2 新的一份
    python 恢复登录状态.py -l love -n 2 --force  跳过确认

★ 只允许在同一线路内恢复。跨线路恢复会把 A 账号的登录态灌进 B 账号，
  app/login_backup.py 里对此有硬拦（目录不在本线路备份根下直接拒绝）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import login_backup, paths                      # noqa: E402
from app.config_store import load_profiles               # noqa: E402


def lines_info() -> list[dict]:
    out = []
    for raw in load_profiles().get("lines", []):
        lid = raw["id"]
        ch = raw.get("chrome", {})
        udd = Path(ch.get("user_data_dir") or paths.chrome_dir(lid))
        out.append({"id": lid, "name": raw.get("name", lid), "udd": udd,
                    "profile": ch.get("profile_directory") or "Default",
                    "backups": login_backup.list_backups(lid)})
    return out


def show_all() -> None:
    infos = lines_info()
    print("线路登录态备份概览（根目录 var\\backups\\<线路>\\）\n")
    any_bak = False
    for it in infos:
        n = len(it["backups"])
        any_bak = any_bak or n > 0
        print(f"[{it['id']}] {it['name']}")
        print(f"    资料目录 : {it['udd']}  存在={it['udd'].is_dir()}")
        print(f"    备份份数 : {n}")
        for i, b in enumerate(reversed(it["backups"]), 1):
            print(f"      {i}. {b['time']}  Cookie {b.get('cookie_count')} 条"
                  f"  {Path(b['path']).name}")
        print()
    if not any_bak:
        print("目前没有任何备份。打开某条线路的窗口时会自动备份一份。")


def show_one(lid: str) -> None:
    it = next((x for x in lines_info() if x["id"] == lid), None)
    if it is None:
        print(f"没有 id 为 {lid} 的线路")
        return
    print(f"[{it['id']}] {it['name']}  资料目录 {it['udd']}")
    for i, b in enumerate(reversed(it["backups"]), 1):
        print(f"  {i}. {b['time']}  Cookie {b.get('cookie_count')} 条  {Path(b['path']).name}")


def restore(lid: str, nth: int, yes: bool) -> int:
    it = next((x for x in lines_info() if x["id"] == lid), None)
    if it is None:
        print(f"没有 id 为 {lid} 的线路")
        return 2
    bs = it["backups"]
    if not bs:
        print(f"{lid} 没有任何备份")
        return 3
    idx = len(bs) - nth
    if idx < 0:
        print(f"{lid} 只有 {len(bs)} 份备份，没有第 {nth} 份")
        return 4
    target = bs[idx]
    pid = login_backup.chrome_busy(it["udd"])
    if pid:
        print(f"这条线的 Chrome 正在运行（PID={pid}），请先关掉它的窗口")
        return 5
    print(f"将把 {target['time']} 这一份恢复到 [{lid}] {it['name']}")
    print(f"  备份内容：Cookie {target.get('cookie_count')} 条 / {target['path']}")
    print(f"  目标目录：{it['udd']}")
    # 确认与「恢复前再存一份」都由 app/safety.py 统一负责：
    # 交互环境会当场问，非交互必须显式 --yes，两种情况都先做完整快照。
    msg = login_backup.restore(lid, target["path"], it["udd"], it["profile"],
                              assume_yes=yes)
    print(msg)
    return 0 if msg.startswith("已恢复") else 6


def main() -> int:
    ap = argparse.ArgumentParser(description="按线路恢复 Chrome 登录态")
    ap.add_argument("-l", "--line", default="", help="线路 id（见 config/profiles.json）")
    ap.add_argument("-n", "--nth", type=int, default=None,
                    help="取第几新的一份（1 = 最新）。不给就只列出备份，不做恢复")
    ap.add_argument("--yes", "--force", dest="yes", action="store_true",
                    help="跳过确认（非交互环境必须加这个）")
    a = ap.parse_args()

    paths.ensure_dirs()
    if not a.line:
        show_all()
        return 0
    show_one(a.line)
    if a.nth is None:
        print("\n要恢复就加 -n 1（或 2、3…）")
        return 0
    return restore(a.line, a.nth, a.yes)


if __name__ == "__main__":
    raise SystemExit(main())
