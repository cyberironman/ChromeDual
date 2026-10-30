# -*- coding: utf-8 -*-
"""删除 Chrome 的本地 AI 模型，并用**不可覆盖的假文件**占住原路径。

为什么这么做
------------
Chrome 127+ 自带端上大模型（Gemini Nano，`OptGuideOnDeviceModel`），
首次触发后**静默下载 4 GB 权重**并常驻占用内存/磁盘；另外还有一批
tflite 小模型（`optimization_guide_model_store`）与语音模型包（`SODALanguagePacks`）。
本项目只用 Chrome 当"带代理的浏览器"，一个都不需要。

做法
----
1. 删掉整个模型目录；
2. 在原路径建一个**同名只读文件**当占位 —— Chrome 想重建同名目录会失败，
   而从外部删一个只读文件也会被 Windows 拒绝，等于把这个位置焊死；
3. 顺手写一条用户级策略 `GenAILocalFoundationalModelSettings=1`（不自动下载本地模型）。

用法
----
    python tools\\chrome_ai_cleanup.py                # 只体检，列出能省多少空间
    python tools\\chrome_ai_cleanup.py --apply        # 真删（会先要求确认）
    python tools\\chrome_ai_cleanup.py --apply --yes  # 脚本里跑，跳过确认
    python tools\\chrome_ai_cleanup.py --include-soda # 连语音模型包一起删
    python tools\\chrome_ai_cleanup.py --only-software# 不动 C 盘日常用的 Chrome

★ 删之前必须确认那一份资料没有 Chrome 在跑，否则文件被占用删不干净。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import paths, safety                            # noqa: E402

# 目录名 → 说明
MODEL_DIRS = {
    "OptGuideOnDeviceModel": "Gemini Nano 端上大模型（通常是 4 GB 的 weights.bin）",
    "optimization_guide_model_store": "优化指南用的 tflite 小模型（文本安全/页面理解等）",
}
SODA_DIRS = {
    "SODALanguagePacks": "SODA 端上语音模型包（实时字幕/语音识别用）",
}

POLICY_KEY = r"Software\Policies\Google\Chrome"
POLICY_NAME = "GenAILocalFoundationalModelSettings"
PLACEHOLDER_TEXT = (
    "此文件由「Chrome 代理管家」创建，用来占住 Chrome 本地 AI 模型的位置。\n"
    "原来的模型目录已被删除（省磁盘、避免 Chrome 常驻占用内存/显卡）。\n"
    "请勿删除本文件 —— 删掉 Chrome 会重新下载几 GB 的模型。\n"
    "确需恢复 Chrome 本地 AI：删掉本文件，并删除注册表\n"
    r"HKCU\Software\Policies\Google\Chrome\GenAILocalFoundationalModelSettings" "\n"
)


def _size(p: Path) -> tuple[int, int]:
    total = files = 0
    for dirpath, _dirnames, filenames in os.walk(p):
        for f in filenames:
            files += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total, files


def _force_rmtree(p: Path) -> None:
    def onerr(func, path, exc):  # noqa: ANN001
        try:
            os.chmod(path, 0o666)
            func(path)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(p, onerror=onerr)


def _roots(only_software: bool) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    base = paths.CHROME_DATA
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                out.append((f"线路 {d.name}", d))
    if not only_software:
        sysdir = paths.system_user_data_dir()
        if sysdir.is_dir():
            out.append(("日常 Chrome（C 盘）", sysdir))
    return out


def scan(only_software: bool, include_soda: bool) -> list[dict]:
    wanted = dict(MODEL_DIRS)
    if include_soda:
        wanted.update(SODA_DIRS)
    rows: list[dict] = []
    for label, root in _roots(only_software):
        for name, desc in wanted.items():
            p = root / name
            if p.is_dir():
                size, files = _size(p)
                rows.append({"label": label, "root": root, "path": p, "name": name,
                             "desc": desc, "size": size, "files": files, "state": "待删"})
            elif p.is_file():
                rows.append({"label": label, "root": root, "path": p, "name": name,
                             "desc": desc, "size": p.stat().st_size, "files": 1,
                             "state": "已占位"})
    return rows


def set_policy(enable: bool = True) -> str:
    if os.name != "nt":
        return "非 Windows，跳过策略"
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, POLICY_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, POLICY_NAME, 0, winreg.REG_DWORD, 1)
                return f"已写入用户策略 HKCU\\{POLICY_KEY}\\{POLICY_NAME}=1（不自动下载本地模型）"
            try:
                winreg.DeleteValue(k, POLICY_NAME)
                return "已删除该策略"
            except FileNotFoundError:
                return "策略本来就不存在"
    except Exception as e:  # noqa: BLE001
        return f"写策略失败（不影响清理结果）：{e}"


def apply(rows: list[dict], assume_yes: bool) -> int:
    todo = [r for r in rows if r["state"] == "待删"]
    if not todo:
        print("没有需要删除的模型目录。")
        return 0

    if not assume_yes:
        if not sys.stdin or not sys.stdin.isatty():
            print("非交互环境，必须显式加 --yes 才会真的删除。")
            return 2
        total = sum(r["size"] for r in todo)
        print(f"\n将删除 {len(todo)} 个目录，释放约 {total / 1048576:.0f} MB。")
        if input("确认删除？输入 yes 继续：").strip().lower() not in ("yes", "y"):
            print("已取消")
            return 1

    freed = 0
    for r in rows:
        if r["state"] == "已占位":
            # 之前已经占过位（可能是旧版本占的，内容只是一句占位符）：
            # 统一改写成能看懂的说明，内容不影响 Chrome 行为。
            p: Path = r["path"]
            try:
                if p.read_text(encoding="utf-8", errors="ignore") != PLACEHOLDER_TEXT:
                    os.chmod(p, 0o666)
                    p.write_text(PLACEHOLDER_TEXT, encoding="utf-8")
                    safety.read_only(p)
                    safety.placeholder_note(p, PLACEHOLDER_TEXT)
                    print(f"  占位说明已更新：{p}")
            except Exception as e:  # noqa: BLE001
                print(f"  占位说明更新失败（不影响禁用）：{p} — {e}")

    for r in todo:
        pid = safety.chrome_busy(r["root"])
        if pid:
            print(f"  跳过 {r['path']}：这份资料正被 Chrome 使用（PID={pid}）")
            continue
        p: Path = r["path"]
        print(f"  删除 {p}（{r['size'] / 1048576:.0f} MB / {r['files']} 个文件）…", end="")
        try:
            _force_rmtree(p)
        except Exception as e:  # noqa: BLE001
            print(f" 失败：{e}")
            continue
        if p.exists():
            print(" 没删干净，跳过占位")
            continue
        try:
            p.write_text(PLACEHOLDER_TEXT, encoding="utf-8")
            safety.read_only(p)
            safety.placeholder_note(p, PLACEHOLDER_TEXT)
        except Exception as e:  # noqa: BLE001
            print(f" 已删但占位失败：{e}")
            continue
        freed += r["size"]
        print(" 完成并已占位")
        safety._audit(f"删除本地 AI 模型 {p}（{r['size'] / 1048576:.0f} MB）")
    print(f"\n共释放约 {freed / 1048576:.0f} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="删除 Chrome 本地 AI 模型 + 只读假文件占位")
    ap.add_argument("--apply", action="store_true", help="真的删（不加只体检）")
    ap.add_argument("--yes", action="store_true", help="跳过确认（脚本用）")
    ap.add_argument("--include-soda", action="store_true", help="连 SODA 语音模型包一起删")
    ap.add_argument("--only-software", action="store_true", help="只处理本软件的四份资料")
    ap.add_argument("--no-policy", action="store_true", help="不要去禁用 Chrome 的模型下载策略")
    a = ap.parse_args()

    paths.ensure_dirs()
    rows = scan(a.only_software, a.include_soda)

    print(f"体检时间 {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    if not rows:
        print("没有找到任何本地 AI 模型目录。")
    total = 0
    for r in rows:
        mark = "已占位" if r["state"] == "已占位" else "待删"
        print(f"[{mark}] {r['label']:<16} {r['size'] / 1048576:8.0f} MB  {r['path']}")
        print(f"         {r['desc']}")
        if r["state"] == "待删":
            total += r["size"]
    print(f"\n合计可释放：{total / 1048576:.0f} MB")

    if not a.apply:
        print("\n（这只是体检。要真删加 --apply）")
        return 0

    rc = apply(rows, a.yes)
    if rc == 0 and not a.no_policy:
        print(set_policy(True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
