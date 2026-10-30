# -*- coding: utf-8 -*-
"""配置的唯一读写入口：config/profiles.json 与 config/targets.json。

以前有两套配置（Tray/profiles.json 和根目录 chrome-proxy.config.json），
改了 A 以为生效了，其实是 B 在起作用。现在只认 config/ 这一份。
"""
from __future__ import annotations

import json
from pathlib import Path

from app import paths
from app.logging_setup import log


def load_profiles(path: Path | None = None) -> dict:
    """读 config/profiles.json，并把 `${ROOT}` 这类占位符展开成真实路径。

    第一次运行（还没有 profiles.json）时，自动从
    config/profiles.example.json 生成一份，用户不需要手工复制文件。
    """
    p = Path(path or paths.PROFILES)
    if not p.is_file():
        if paths.ensure_profiles():
            log.info("首次运行：已从 profiles.example.json 生成 %s", p.name)
        else:
            raise FileNotFoundError(
                f"找不到 {p}，也没有 {paths.PROFILES_EXAMPLE.name} 可以生成。"
                "（这两个文件都在 config\\ 目录下）")
    raw = json.loads(p.read_text(encoding="utf-8"))
    return paths.expand(raw)


def save_profiles(data: dict, path: Path | None = None) -> None:
    p = Path(path or paths.PROFILES)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_targets(path: Path | None = None) -> list[dict]:
    p = Path(path or paths.TARGETS)
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("targets", [])
    except Exception as e:  # noqa: BLE001
        log.warning("targets.json 解析失败：%s", e)
        return []


def read_startup_urls(user_data_dir: str | Path | None, profile: str = "Default") -> list[str]:
    """读 Chrome「启动时打开特定网页」里的网址列表（仅兜底）。

    坑：新版 Chrome 把这条设置写进受保护的 Secure Preferences，
    普通 Preferences 里 session 段是空的 —— 只读 Preferences 会以为没设置。
    ⚠ 但 Chrome 对该段有 HMAC 校验，外部写进去会被还原，
      所以权威来源是 config/profiles.json 的 chrome.startup_urls。
    """
    if not user_data_dir:
        return []
    base = Path(user_data_dir)
    prof = profile or "Default"
    for name in ("Secure Preferences", "Preferences"):
        f = base / prof / name
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:  # noqa: BLE001
            continue
        sess = data.get("session") or {}
        if sess.get("restore_on_startup") != 4:      # 4 = 打开特定网页
            continue
        urls = [u for u in (sess.get("startup_urls") or [])
                if isinstance(u, str) and u.strip()]
        if urls:
            return urls
    return []
