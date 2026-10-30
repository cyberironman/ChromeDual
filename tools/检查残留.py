# -*- coding: utf-8 -*-
"""发布前自检：这个仓库里有没有混进「只属于我这台机器」的东西。

为什么需要它
------------
个人项目转开源，最容易漏的不是代码，是**开发过程中留下的痕迹**：

1. 被 `.gitignore` 写了、但**早就在版本控制里**的文件 —— gitignore 对已跟踪文件
   完全无效，你以为忽略了，其实每次都跟着提交（这个坑踩过两次）。
2. 注释和 docstring 里随口举的例子 —— 「D:\\ChromeDual\\Love」「-l love」这种，
   代码逻辑没错，但把自己真实线路名和盘托出去了。
3. 订阅地址、网址名单、真实配置。

用法
----
    python tools\\检查残留.py            # 有问题退出码 1
    python tools\\检查残留.py --all      # 连没被跟踪的文件一起看
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 只在这些后缀里找文本（别去读 ico / zip）
TEXT_SUFFIX = {".py", ".md", ".json", ".txt", ".pac", ".bat", ".html", ".spec",
               ".yml", ".yaml", ".cfg", ".ini", ".ps1", ".cmd"}

#: 明确的个人信息特征。想加自己的，就在这儿追加。
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("绝对路径", re.compile(r"\b[A-Za-z]:\\\\?[A-Za-z0-9_]")),
    # 只认「像真地址」的：scheme 后面至少 16 个非空白字符。
    # 这样代码里的 startswith("vless://") 不会误报，真订阅链接一定跑不掉。
    ("订阅链接", re.compile(r"\b(vless|vmess|trojan|ssr?|hysteria2?)://[^\s\"'\\]{16,}", re.I)),
    ("个人站点", re.compile(r"chineseinla|xiaolanshu|小蓝书", re.I)),
    ("真实线路名", re.compile(r"\b(love|fastus|xls)\b", re.I)),
]

#: 这些是「本来就该出现」的路径示例，不算泄漏
ALLOW = (
    "# 用法", "用法：", "示例", "例如",
    "D:\\\\Apps",                 # 安装器帮助里的通用示例路径
    "C:\\Python", "D:\\Python311",  # 讲「不要写死解释器路径」的说明文字
    "Program Files",              # Chrome 的标准安装位置，不属于个人信息
    "${ROOT}", "<数据目录>",
)


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                         capture_output=True, text=True)
    return [x for x in out.stdout.splitlines() if x.strip()]


def tracked_but_ignored() -> list[str]:
    """★ 最关键的一项：写进 .gitignore、却仍然被跟踪的文件。"""
    out = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard"],
                         cwd=str(ROOT), capture_output=True, text=True)
    return [x for x in out.stdout.splitlines() if x.strip()]


def scan(paths: list[str]) -> list[tuple[str, int, str, str]]:
    hits = []
    for rel in paths:
        p = ROOT / rel
        if p.suffix.lower() not in TEXT_SUFFIX or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if any(a in line for a in ALLOW):
                continue
            for label, rx in PATTERNS:
                if rx.search(line):
                    hits.append((rel, no, label, line.strip()[:96]))
                    break
    return hits


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    files = tracked() if "--all" not in argv else [
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in ROOT.rglob("*") if p.is_file()
        and ".git" not in p.parts and "__pycache__" not in p.parts
        and not p.name.endswith(".pyc")
    ]

    bad = tracked_but_ignored()
    hits = scan(files)

    print(f"检查范围：{len(files)} 个文件")
    if bad:
        print()
        print(f"[!] 有 {len(bad)} 个文件写进了 .gitignore 却仍被跟踪"
              f"（gitignore 对已跟踪文件无效）：")
        for f in bad:
            print(f"    {f}")
        print("    修法：git rm --cached <文件>")
    if hits:
        print()
        print(f"[!] 有 {len(hits)} 处疑似个人信息：")
        for rel, no, label, text in hits:
            print(f"    {rel}:{no}  [{label}]  {text}")
    if not bad and not hits:
        print()
        print("干净：没有已跟踪的忽略文件，也没有绝对路径 / 订阅链接 / 真实线路名。")
        return 0

    print()
    print("请改完再发布。内容类误报可以加到 PATTERNS 的 ALLOW 里。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
