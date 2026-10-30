# -*- coding: utf-8 -*-
"""日志：全项目共用一个 logger（"guard"），所有模块 import 到的都是它。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app import paths

log = logging.getLogger("guard")

FMT = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")


def setup_logging(verbose: bool = False) -> None:
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        log.removeHandler(h)
    fh = RotatingFileHandler(paths.LOGS / "guard.log", maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(FMT)
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)
    if verbose and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(FMT)
        sh.setLevel(logging.DEBUG)
        log.addHandler(sh)
