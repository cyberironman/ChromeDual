# -*- coding: utf-8 -*-
"""⚠ 兼容外壳 —— 面板代码已移到 D:\\ChromeDual\\app\\panel.py，页面在 app\\panel.html。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.panel import Api, Panel, apply_icon  # noqa: E402,F401

__all__ = ["Api", "Panel", "apply_icon"]
