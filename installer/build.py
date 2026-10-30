# -*- coding: utf-8 -*-
"""一键构建：主程序 exe → 安装器 exe → 绿色版 zip，全部落到 release\\。

    python installer\\build.py              # 全打
    python installer\\build.py --skip-setup # 只打主程序（调试用，快）
    python installer\\build.py --portable   # 额外压一个免安装 zip

产物
----
    release\\ChromeDual-Setup-<版本>.exe        给用户双击的那个（推荐）
    release\\ChromeDual-<版本>-portable.zip     免安装版（解压就能跑）
    release\\SHA256SUMS.txt                     校验和（发 Release 时贴上去）

为什么要单独一个脚本而不是让人记两串 PyInstaller 命令
------------------------------------------------------
两条命令有先后依赖（安装器要把主程序的 dist 塞进自己体内），
顺序错了报的错还特别难看（"找不到 dist\\ChromeDual\\ChromeDual.exe"）。
这里把顺序、清理、改名、算校验和全包掉，谁执行都一样。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = ROOT / "release"


def _fresh(base: Path) -> Path:
    """准备一个空的输出目录。

    PyInstaller 遇到已存在的输出目录会先整棵删掉。在受管环境（企业策略、
    沙箱、某些杀软）里，这种「一次删几百个文件」的操作会被拦下来，构建就
    以一个跟代码毫无关系的错误结束。这里先自己试着删，删不掉就把旧目录
    改名让路 —— 改名几乎不会被拦，构建就能继续。
    """
    if not base.exists():
        return base
    try:
        shutil.rmtree(base)
    except Exception:  # noqa: BLE001
        pass
    # ⚠ 不能只靠 rmtree 抛不抛异常来判断 —— 受管安全策略可能「静默跳过删除、
    #   只在控制台打一行提示」，目录其实还在。以「目录还在不在」为准。
    if not base.exists():
        return base
    alt = base.with_name(f"{base.name}_old_{time.strftime('%H%M%S')}")
    try:
        base.rename(alt)
        print(f"  [!] {base.name} 清不掉，已改名让路 -> {alt.name}")
    except OSError as e:
        raise SystemExit(f"[失败] 无法清理 {base}：{e}") from e
    return base


def version() -> str:
    """版本号的唯一来源：installer/version_info.txt 里的 FileVersion。"""
    txt = (INSTALLER / "version_info.txt").read_text(encoding="utf-8")
    m = re.search(r"filevers=\((\d+),\s*(\d+),\s*(\d+)", txt)
    return "{}.{}.{}".format(*m.groups()) if m else "0.0.0"


def banner(text: str) -> None:
    print()
    print("=" * 62)
    print("  " + text)
    print("=" * 62)


def run(cmd: list[str], env: dict | None = None) -> None:
    print("  $ " + " ".join(cmd))
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise SystemExit(f"\n[失败] 上一条命令退出码 {r.returncode}")
    print(f"  -> {time.time() - t0:.1f}s")


def pyinstaller(spec: Path, distpath: Path, env: dict | None = None) -> None:
    # workpath 按 spec 名分开：主程序和安装器各自一份，互不覆盖，也不会
    # 因为「第二次构建把第一次的中间产物清掉」而重新分析一遍。
    run([sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", str(distpath), "--workpath", str(_fresh(BUILD / spec.stem)),
         str(spec)],
        env=env)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_zip(src: Path, out: Path) -> None:
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, Path(src.name) / p.relative_to(src))


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 ChromeDual 的 exe 与安装包")
    ap.add_argument("--skip-setup", action="store_true", help="只打主程序，不打安装器")
    ap.add_argument("--portable", action="store_true", help="额外生成免安装 zip")
    args = ap.parse_args()

    ver = version()
    RELEASE.mkdir(parents=True, exist_ok=True)

    banner(f"ChromeDual {ver} · 1/2 主程序（onedir）")
    out = _fresh(DIST)
    pyinstaller(INSTALLER / "ChromeDual.spec", out)
    main_dir = out / "ChromeDual"
    main_exe = main_dir / "ChromeDual.exe"
    if not main_exe.is_file():
        raise SystemExit("[失败] 没生成 dist\\ChromeDual\\ChromeDual.exe")

    # 冒烟测试：exe 能不能说出自己的依赖状况。这比「双击看它有没有图标」靠谱。
    banner("冒烟测试：ChromeDual.exe --check")
    check = main_dir / "var" / "logs" / "deps-check.txt"
    try:
        check.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        subprocess.run([str(main_exe), "--check"], timeout=120, cwd=str(main_dir))
    except subprocess.TimeoutExpired:
        print("  [!] --check 超时（不影响打包，但建议手动跑一次看看）")
    except OSError as e:
        print(f"  [!] 起不来：{e}")
    if check.is_file():
        print("  -- 自检报告 --")
        for line in check.read_text(encoding="utf-8").splitlines():
            print("  " + line)
    else:
        print("  [!] 没写出自检报告，可能启动就崩了 —— 先手动跑 dist\\ChromeDual\\ChromeDual.exe")

    setup_exe: Path | None = None
    if not args.skip_setup:
        banner(f"ChromeDual {ver} · 2/2 安装程序（onefile）")
        env = dict(os.environ, CHROMEDUAL_DIST=str(main_dir))
        pyinstaller(INSTALLER / "ChromeDual-Setup.spec", out, env=env)
        built = out / "ChromeDual-Setup.exe"
        if not built.is_file():
            raise SystemExit("[失败] 没生成 ChromeDual-Setup.exe")
        setup_exe = RELEASE / f"ChromeDual-Setup-{ver}.exe"
        shutil.copy2(built, setup_exe)

    portable: Path | None = None
    if args.portable:
        banner("免安装 zip")
        portable = RELEASE / f"ChromeDual-{ver}-portable.zip"
        print("  压缩中（90 MB 左右，要一会儿）…")
        make_zip(main_dir, portable)

    banner("完成")
    lines = []
    for p in (setup_exe, portable):
        if p and p.is_file():
            lines.append(f"{sha256(p)}  {p.name}")
            print(f"  {p.name:<40} {human(p.stat().st_size)}")
    if lines:
        (RELEASE / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print()
        print("  校验和已写入 release\\SHA256SUMS.txt")
    print()
    print(f"  发行产物目录：{RELEASE}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
