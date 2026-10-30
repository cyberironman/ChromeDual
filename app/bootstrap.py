# -*- coding: utf-8 -*-
"""首次运行的「兜底配置」—— 让 clone 下来就能先把软件跑起来。

场景：
    你刚 clone 了这个仓库，还没有任何线路的内核配置、也没有订阅地址。
    正常逻辑会直接报错「配置文件不存在」，看起来像软件坏了。

这里做的事：
    给这条线路生成一份**最小的 xray 配置**（http 入口 + freedom 直连出口），
    端口用本次随机分配到的值。这样：
      · 软件能起来，窗口能开，托盘能点，不至于第一步就卡死；
      · 但它**不翻墙**（直连），日志和面板都会明确告诉你「这是占位配置」。

真正的代理怎么来：
    1) 有现成的 xray 配置 → 直接放到 var\\runtime\\<线路>\\xray-runtime.json
    2) 有订阅地址 → python tools\\build_balancer_config.py --root data\\source\\<线路> --sub-url "<地址>"
    3) 两条都完成后，重开该线路即可（管家会重新生成并加载）。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.logging_setup import log


def placeholder_config(http_port: int, socks_port: int = 0) -> dict:
    """一份最小可用的 xray 配置：http 入口 + 直连出口。"""
    socks_port = socks_port or (http_port + 1 if http_port else 0)
    inbounds = [{
        "tag": "http-in",
        "listen": "127.0.0.1",
        "port": int(http_port or 0),
        "protocol": "http",
        "settings": {"timeout": 30},
    }]
    if socks_port:
        inbounds.append({
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "port": int(socks_port),
            "protocol": "socks",
            "settings": {"udp": True},
        })
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
        "routing": {"rules": [{
            "type": "field",
            "inboundTag": [i["tag"] for i in inbounds],
            "outboundTag": "direct",
        }]},
    }


def ensure_placeholder(line) -> bool:
    """给这条线路写一份占位配置；成功返回 True。

    ★ 绝不覆盖已有配置 —— 用户自己的配置优先级最高。
    """
    cfg: Path = line.config
    if cfg.exists():
        return True
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        data = placeholder_config(line.http_port, line.socks_port)
        tmp = cfg.with_name(cfg.name + ".new")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cfg)
    except Exception as e:  # noqa: BLE001
        log.error("[%s] 生成占位配置失败：%s", getattr(line, "name", "?"), e)
        return False

    log.warning("[%s] 这是一份**占位配置**（直连，不翻墙）。"
                "要真代理请二选一：① 把现成的 xray 配置放到 %s；"
                "② 跑 tools\\build_balancer_config.py --sub-url \"<你的订阅地址>\"。",
                getattr(line, "name", "?"), cfg)
    return True
