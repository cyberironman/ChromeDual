# -*- coding: utf-8 -*-
"""总控：线路集合、PAC 服务、保活、重点网址巡检。

它只做「调度」，具体一件事一件事都拆在别的模块里：
    打开 Chrome / 起内核 → line.py
    端口与站点探测      → net.py
    进程识别            → procs.py
    PAC 静态服务        → pac.py
    配置读取            → config_store.py
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from app import login_guard
from app import pac as pac_mod
from app import paths, ports
from app.config_store import load_profiles, load_targets
from app.line import Line
from app.logging_setup import log, setup_logging
from app.net import port_listeners, probe_target
from app.pac import PacServer
from app.procs import chrome_instances, norm_path


class Guard:
    def __init__(self, profiles_path: Path | None = None):
        self.profiles_path = Path(profiles_path or paths.PROFILES)
        self.raw = load_profiles(self.profiles_path)
        d = self.raw.get("defaults", {})
        self.chrome_exe: str = d.get("chrome") or str(paths.CHROME_EXE)
        self.health_url: str = d.get("health_url", "https://www.gstatic.com/generate_204")
        self.keepalive: int = int(d.get("keepalive_seconds", 30))
        # 窗口全关之后，内核还留多久（秒）。到点就停，尽量不占端口、不占内存。
        self.idle_stop: int = int(d.get("idle_stop_seconds", 120))
        self.lines = [Line(r, d) for r in self.raw.get("lines", [])]

        self.pac = None
        self._pac_src: Path | None = None
        ps = self.raw.get("pac_server")
        if ps:
            pdir = Path(ps.get("directory") or paths.PAC_DIR)
            if not pdir.is_absolute():
                pdir = paths.ASSETS / pdir
            self._pac_src = pdir
            self.pac = PacServer(pdir, int(ps["port"]))

        self._alive = False
        self._thread: threading.Thread | None = None
        self._busy = threading.Lock()

        self.targets: list[dict] = load_targets()
        if self.targets:
            log.info("已加载 %d 个重点网址", len(self.targets))
        self._target_state: dict[str, dict] = {}
        self._target_thread: threading.Thread | None = None
        self._heal_at: dict[str, float] = {}

        # ★ 端口随机分配（见 app/ports.py）：必须在建完 lines / pac 之后、
        #   任何人读 http_port 之前做完，否则会拿着写死的老端口去开窗。
        self._assign_ports()
        self._check_login_paths()

    # ------------------------------------------------------ 资料目录冻结校验
    def _check_login_paths(self) -> None:
        """启动第一件事：确认每条线路的资料目录还是**原来那个路径**。

        为什么值得单独一步：Chrome 127+ 的 v20 Cookie 用 ABE（应用绑定加密）
        把 user-data-dir 的绝对路径写进了密文里。路径一改（复制目录、改名、
        换盘符），Chrome 解不开就**静默删掉**这批 Cookie —— 用户看到的就是
        「所有网站同时退出登录」，而且日志里一个字都没有。

        所以路径对不上必须大声报出来，并留档，而不是默默继续开窗。
        """
        self.login_issues: list[str] = []
        for l in self.lines:
            try:
                r = login_guard.freeze(l.id, l.user_data_dir)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 资料目录校验跳过：%s", l.name, e)
                continue
            if r.get("ok"):
                continue
            msg = (f"{l.name} 的资料目录被改过：原来 {r.get('old_path')}，"
                   f"现在指向 {r.get('path')}。绑在原路径上的加密 Cookie 会全部作废。")
            self.login_issues.append(msg)
            log.error("[登录守门] %s", msg)

    # ------------------------------------------------------------ 端口分配
    def _assign_ports(self) -> None:
        """给每条线路、PAC 服务分配端口：随机 + 持久化 + 冲突自动重挑。

        `ours` 用来识别「自家遗留内核」——上次管家退出时内核可能还占着端口，
        那是可以沿用的，不能当成外部冲突把它踢掉（踢掉就会换端口、掉窗口）。
        """
        wanted: dict[str, int] = {}
        paired: list[str] = []
        for l in self.lines:
            if l.use_pac or not l.enabled:
                continue
            slot = f"line:{l.id}"
            wanted[slot] = l.http_port
            paired.append(slot)
        if self.pac is not None:
            wanted["pac"] = self.pac.port
        if not wanted:
            return

        cfg_paths = {norm_path(l.config) for l in self.lines}
        me = os.getpid()

        def ours(port: int) -> bool:
            for pid, cmd in port_listeners(port):
                if pid == me:
                    return True
                c = norm_path(cmd)
                if ("xray" in c or "sing-box" in c) and \
                        any(cf and cf in c for cf in cfg_paths):
                    return True
            return False

        try:
            got = ports.assign(wanted, ours=ours, paired=paired)
        except Exception as e:  # noqa: BLE001
            log.error("端口分配失败，退回配置原值：%s", e)
            return

        for l in self.lines:
            p = got.get(f"line:{l.id}")
            if p and not l.use_pac:
                l.set_http_port(p)
                l.sync_config_ports()
        if self.pac is not None and got.get("pac"):
            self.pac.port = int(got["pac"])
        for l in self.lines:      # PAC 线路的 --proxy-pac-url 必须跟着走
            if l.use_pac and self.pac is not None:
                l.chrome["pac_port"] = self.pac.port
        ports.release_legacy_slots("line:", [f"line:{l.id}" for l in self.lines])

    def line(self, lid: str) -> Line | None:
        return next((l for l in self.lines if l.id == lid), None)

    # ------------------------------------------------------------ 全量启停
    # PAC 模板里写死的历史端口，只在渲染时当作「要替换掉的键」，**不是实际端口**。
    # 实际端口每次由 app/ports.py 随机分配（20000-39998），所以这几个数字
    # 出现在代码里是安全的：随机段和 108xx 不重叠，永远不会跟真端口相等。
    # 给它们起名字，免得后来的人误读成「这里还用 10888」。
    TPL_VIDEO_PORT = 10888      # 模板里视频线路的旧端口
    TPL_DEFAULT_PORT = 10898    # 模板里默认线路的旧端口
    TPL_PAC_PORT = 10890        # 模板里 PAC 自己的旧端口

    def _render_pac(self) -> Path:
        """把 PAC 模板按「这次随机到的端口」渲染一份，返回可直接服务的目录。

        模板里那两个端口是写死的历史值，端口随机化以后必须现渲染，
        否则 PAC 线路拿到的脚本会把流量指向没人听的端口（表现为全部超时）。
        """
        ps = self.raw.get("pac_server") or {}
        video = self.line(str(ps.get("video_line") or "")) if ps else None
        dflt = self.line(str(ps.get("default_line") or "")) if ps else None
        # 没配（或 id 写错）就自动取「前两条走代理的线路」，不依赖任何具体名字。
        proxied = [l for l in self.lines if not l.use_pac]
        if video is None and proxied:
            video = proxied[0]
        if dflt is None and len(proxied) > 1:
            dflt = proxied[1]
        slots: dict[int, int] = {self.TPL_PAC_PORT: self.pac.port}
        tokens: dict[str, str] = {}
        if video is not None and video.http_port:
            tokens["__PORT_VIDEO__"] = str(video.http_port)
            slots[self.TPL_VIDEO_PORT] = video.http_port
        if dflt is not None and dflt.http_port:
            tokens["__PORT_DEFAULT__"] = str(dflt.http_port)
            slots[self.TPL_DEFAULT_PORT] = dflt.http_port
        try:
            return pac_mod.render(self._pac_src or paths.PAC_DIR,
                                  paths.RUNTIME / "pac", slots, tokens)
        except Exception as e:  # noqa: BLE001
            log.error("PAC 渲染失败，退回模板目录（端口可能对不上）：%s", e)
            return paths.PAC_DIR

    def ensure_pac(self) -> None:
        """PAC 静态服务按需启（只在有人要用 PAC 线路时才占端口）。"""
        if self.pac is None:
            return
        if not self.pac.running:
            self.pac.directory = self._render_pac()
            self.pac.start()

    def ensure_line(self, lid: str) -> bool:
        """**按下时按需**：只有这条线要开窗口，才把它自己的内核拉起来。

        这是 2026-10-30 用户明确要求的行为 ——「启动哪个 chrome 再启动对应代理」。
        管家启动本身**不再**预热任何内核，也不碰系统代理。
        """
        line = self.line(lid)
        if line is None or not line.enabled:
            return False
        line.desired = True
        if line.use_pac:
            self.ensure_pac()
            return True
        st = line.status()
        if st["state"] in ("running", "orphan"):
            return True
        log.info("[%s] 按需启动：准备内核（端口 %s）", line.name, line.http_port)
        return line.start()

    def start_all(self) -> None:
        """用户**显式**点「启动全部线路」才走这里（菜单/面板按钮）。"""
        self.ensure_pac()
        for l in self.lines:
            if l.enabled and not l.use_pac:
                l.desired = True
                try:
                    l.start()
                except Exception as e:  # noqa: BLE001
                    log.exception("[%s] 启动异常：%s", l.name, e)
        self.start_keepalive()

    def start_passive(self) -> None:
        """管家启动时的默认姿态：**一个内核都不起**，只开始盯状态。

        以前这里调 start_all()，于是软件一开机就把三条内核全拉起来，
        对应的 Chrome 一个都没开 —— 用户看到的就是「先开了一堆代理」。
        """
        for l in self.lines:
            l.desired = False
        self.start_keepalive()
        log.info("按需启动模式：等哪个窗口要用，才起那条线的内核（空闲 %s 秒后自动停）",
                 self.idle_stop)

    def stop_all(self) -> None:
        self._alive = False
        for l in self.lines:
            l.stop()
        if self.pac:
            self.pac.stop()

    def restart_all(self) -> None:
        with self._busy:
            for l in self.lines:
                if l.enabled and not l.use_pac:
                    l.stop()
            for l in self.lines:
                if l.enabled and not l.use_pac:
                    l.desired = True
                    try:
                        l.start(rebuild=True)
                    except Exception as e:  # noqa: BLE001
                        log.exception("[%s] 重启异常：%s", l.name, e)

    # ---------------------------------------------------------- 重点网址
    def _ordered_lines(self, target: dict) -> list[Line]:
        prefer = [str(x) for x in (target.get("prefer") or [])]
        usable = [l for l in self.lines if l.enabled and not l.use_pac]
        head = [l for pid in prefer for l in usable if l.id == pid]
        return head + [l for l in usable if l not in head]

    def probe_one_target(self, target: dict, autostart: bool = False) -> dict:
        """对单个重点网址逐条线路实测，第一个真正能打开的即为当前最优线路。

        autostart=True 时，遇到没跑但配置可用的线路会**临时起一下再测**
        （这正是「打开某个网址 → 顺带把它需要的代理起起来」）；
        返回结果里 `spawned` 记录本次为了探测而起的线路，调用方用完可以关掉。
        """
        url = target["url"]
        min_bytes = int(target.get("min_bytes", 20000))
        must = target.get("must_contain", "") or ""
        tried: list[str] = []
        spawned: list[str] = []
        for l in self._ordered_lines(target):
            # 这里只关心「这条线能不能把站点打开」，不是「它归不归管家管」。
            # 所以端口通就试 —— 刚起来的（starting）也算，否则一启动就误判全线不通，
            # 白白触发一次节点重建（踩过：面板显示某条线未运行、其实只是还没起完）。
            if not l.port_up() and autostart and l.enabled:
                if self._busy.locked():
                    tried.append(f"{l.name}:正在忙，先跳过")
                    continue
                log.info("重点网址 %s：按需启动 [%s] 再测", target["id"], l.name)
                if l.start():
                    spawned.append(l.id)
            if not l.port_up():
                tried.append(f"{l.name}:未启动")
                continue
            r = probe_target(l.http_port, url, min_bytes, must)
            if r["ok"]:
                log.info("重点网址 %s → 走 [%s] %s KB / %ss", url, l.name,
                         r["bytes"] // 1024, r["cost"])
                return {"id": target["id"], "name": target.get("name", target["id"]),
                        "url": url, "ok": True, "line": l.id, "line_name": l.name,
                        "port": l.http_port, "bytes": r["bytes"], "cost": r["cost"],
                        "detail": f"走 {l.name} · {r['bytes'] // 1024} KB · {r['cost']}s",
                        "tried": tried, "spawned": spawned}
            tried.append(f"{l.name}:{r['why']}")
            log.debug("重点网址 %s 走 [%s] 不通：%s", url, l.name, r["why"])
        log.warning("重点网址 %s 当前所有线路都打不开：%s", url, "；".join(tried[-6:]))
        return {"id": target["id"], "name": target.get("name", target["id"]),
                "url": url, "ok": False, "line": "", "line_name": "", "port": 0,
                "bytes": 0, "cost": 0,
                "detail": "所有线路都打不开（空壳响应/超时）", "tried": tried,
                "spawned": spawned}

    def _target_loop(self) -> None:
        interval = int(self.raw.get("defaults", {}).get("target_interval", 120))
        while self._alive:
            for t in self.targets:
                if not self._alive:
                    return
                try:
                    st = self.probe_one_target(t)
                    self._target_state[t["id"]] = st
                    if not st["ok"]:
                        self._maybe_heal(t)
                except Exception as e:  # noqa: BLE001
                    log.exception("重点网址探测异常：%s", e)
            for _ in range(interval * 2):
                if not self._alive:
                    return
                time.sleep(0.5)

    def _maybe_heal(self, target: dict) -> None:
        """重点网址全线打不开时，让该线路**重新挑节点**（这就是「换更好的链接」）。

        只对 targets.json 里写了 rebuild 的条目生效，并且 30 分钟内最多一次 ——
        重建要重跑节点探活/测速，很重，不能每轮都来。
        """
        lid = target.get("rebuild")
        if not lid:
            return
        now = time.time()
        if now - self._heal_at.get(target["id"], 0) < 1800:
            log.debug("重点网址 %s 已重建过，30 分钟内不再重建", target["id"])
            return
        line = self.line(str(lid))
        if line is None or not line.enabled:
            return
        # ★ 刚重启不到 90 秒的线路还没握手稳，这时候判「打不开」是冤枉它。
        #   踩过：内核刚起 3 秒就被判全线不通 → 白烧一次重建机会，把好节点换掉。
        if line._started_at and time.time() - line._started_at < 90:
            log.info("重点网址 %s：线路 [%s] 刚启动 %.0f 秒，先不重建，等下一轮",
                     target["id"], line.name, time.time() - line._started_at)
            return
        self._heal_at[target["id"]] = now
        log.warning("重点网址 %s 全线打不开 → 重建线路 [%s] 的节点选择", target["id"], line.name)
        # 先删探测缓存，逼它重新实测（缓存默认 6 小时，不清等于不重建）
        cache = line.source / "region-probe.json"
        if cache.exists():
            try:
                cache.unlink()
                log.info("已清除探测缓存 %s", cache)
            except OSError as e:
                log.warning("清除缓存失败：%s", e)

        def work() -> None:
            try:
                with self._busy:
                    line.start(rebuild=True)
            except Exception as e:  # noqa: BLE001
                log.exception("重建线路失败：%s", e)

        threading.Thread(target=work, name="heal-" + target["id"], daemon=True).start()

    def start_target_probe(self) -> None:
        if not self.targets:
            return
        if self._target_thread and self._target_thread.is_alive():
            return
        self._target_thread = threading.Thread(target=self._target_loop,
                                               name="target-probe", daemon=True)
        self._target_thread.start()
        log.info("重点网址巡检已开启")

    def target_best(self, tid: str, autostart: bool = False) -> dict | None:
        st = self._target_state.get(tid)
        if st and st.get("ok"):
            return st
        t = next((x for x in self.targets if x["id"] == tid), None)
        if t is None:
            return None
        fresh = self.probe_one_target(t, autostart=autostart)
        self._target_state[tid] = fresh
        return fresh if fresh["ok"] else None

    def targets_status(self) -> list[dict]:
        out = []
        for t in self.targets:
            st = self._target_state.get(t["id"])
            out.append(st or {"id": t["id"], "name": t.get("name", t["id"]),
                              "url": t["url"], "ok": False, "line": "", "line_name": "",
                              "port": 0, "bytes": 0, "cost": 0,
                              "detail": "未检测（按这个网址打开时会自动实测）", "tried": []})
        return out

    def open_target(self, tid: str) -> str:
        """按重点网址打开窗口：自动选当前**真正能打开它**的那条线路。

        这就是「让这个链接优先」——不是把某个节点写死，而是每次都实测一遍，
        谁能打开就用谁。没跑的线路会临时起一下测，测完没被选中的再关掉，
        避免为了一个网址把三个内核全留在后台。
        """
        t = next((x for x in self.targets if x["id"] == tid), None)
        if t is None:
            return f"没有 id 为 {tid} 的重点网址"
        before = {l.id: l.port_up() for l in self.lines}
        best = self.target_best(tid, autostart=True)
        if best is None:
            # 探测期间起的临时内核用完就关（保持「不用不占」）
            for l in self.lines:
                if not before.get(l.id) and l.port_up() and not l.windows():
                    l.stop()
            return f"{t.get('name', tid)}：当前所有线路都打不开（换节点或等它恢复）"
        line = self.line(best["line"])
        if line is None:
            return f"{t.get('name', tid)}：找不到线路 {best['line']}"
        if not line.use_pac and line.status()["state"] not in ("running", "orphan"):
            line.start()
        msg = line.open_chrome(self.chrome_exe, url=t["url"])
        # 只为探测而起的其他线路，收工
        for l in self.lines:
            if l.id == line.id:
                continue
            if not before.get(l.id) and l.port_up() and not l.windows():
                log.info("[%s] 探测已完成、没有窗口在用，停掉内核", l.name)
                l.stop()
        return msg

    # ---------------------------------------------------------------- 保活
    def start_keepalive(self) -> None:
        # 先置活标志：stop_all() 会把它置 False，线程虽已退出但可能还没完全结束，
        # 以前「判断线程还活着就直接 return」，于是点了「停止」再点「启动」后
        # 保活永远不恢复（线路挂了没人管）。先置 True 再判断。
        self._alive = True
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while self._alive:
                for _ in range(self.keepalive * 2):
                    if not self._alive:
                        return
                    time.sleep(0.5)
                now = time.time()
                for l in self.lines:
                    if not (l.enabled and not l.use_pac):
                        continue

                    wins = l.windows()
                    if wins:
                        # 窗口还在 → 这条线就是在用，顺便刷新「最后见到窗口的时间」
                        l.desired = True
                        l.last_window_seen = now

                    # ① 没人要的线路：不启动、不重拉、不探测，已有的内核直接收掉。
                    #    这就是「最大程度不影响其他软件」—— 不用就不占端口不占内存。
                    if not l.desired:
                        if l.port_up() or l.kernel_alive():
                            log.info("[%s] 没有窗口在用，停掉内核（按需模式）", l.name)
                            l.stop()
                        continue

                    st = l.status()
                    if st["state"] in ("running", "orphan"):
                        # orphan 虽然不在管家名下，但内核确实在提供服务，
                        # **绝不能因为「不归我管」就去杀它重启**。
                        if l._miss:
                            log.info("[%s] 已恢复，清零计数", l.name)
                        l._miss = 0
                        l._backoff = 60.0
                        l._next_retry = 0.0
                        # ② 窗口全关了、且超过空闲时长 → 停内核，回到「没开就不跑」
                        if (not wins and l.last_window_seen
                                and now - l.last_window_seen > self.idle_stop):
                            log.info("[%s] 窗口已关闭 %.0f 秒，停掉内核（空闲回收）",
                                     l.name, now - l.last_window_seen)
                            l.stop()
                        continue
                    if st["state"] == "foreign":
                        # 端口被别的程序占着，重拉只会失败；降频提示，不折腾
                        if now >= l._next_retry:
                            log.warning("[%s] %s，暂不重拉", l.name, st["detail"])
                            l._next_retry = now + 120
                        continue
                    if self._busy.locked():
                        continue

                    l._miss += 1
                    # ★ 端口没响应 ≠ 内核死了。全机 socket 紧张时连本地端口都会超时，
                    #   以前这里直接「杀掉重拉」，把还活着的内核杀了又起新的，
                    #   两头抢资源越修越坏（2026-10-29 20:17-20:37 每 30 秒一次）。
                    if l.kernel_alive() and l._miss < 3:
                        log.debug("[%s] 端口 %s 暂时无响应（第 %s 次），内核仍在，先不动",
                                  l.name, l.http_port, l._miss)
                        continue
                    if l.kernel_alive():
                        log.info("[%s] 端口连续 %s 次无响应，内核虽在但已不工作，重启",
                                 l.name, l._miss)
                    else:
                        log.warning("[%s] 内核已退出，重新拉起", l.name)

                    if now < l._next_retry:
                        log.debug("[%s] 退避中，%.0f 秒后再试", l.name, l._next_retry - now)
                        continue
                    try:
                        ok = l.start()
                    except Exception as e:  # noqa: BLE001
                        log.exception("[%s] 自动重拉失败：%s", l.name, e)
                        ok = False
                    if ok:
                        l._miss = 0
                        l._backoff = 60.0
                        l._next_retry = 0.0
                    else:
                        l._backoff = min(l._backoff * 2, 300.0)
                        l._next_retry = time.time() + l._backoff
                        log.warning("[%s] 重拉失败，%s 秒后再试", l.name, int(l._backoff))

        self._thread = threading.Thread(target=loop, name="keepalive", daemon=True)
        self._thread.start()
        log.info("保活已开启，间隔 %s 秒", self.keepalive)

    # ------------------------------------------------------------ 状态快照
    def snapshot(self) -> list[dict]:
        inst = chrome_instances()
        out = []
        for l in self.lines:
            st = l.status()
            udd = l.user_data_dir
            wins = inst.get(norm_path(udd), [])
            out.append({"id": l.id, "name": l.name, "ok": st["ok"], "pac": l.use_pac,
                        "port": st["port"], "state": st["state"],
                        "pids": st["pids"], "detail": st["detail"],
                        "windows": len(wins), "window_pids": wins,
                        # 「是否被需要」：false 表示按需模式下属待机，内核本来就不该跑，
                        # 面板不要把它显示成「故障」。
                        "desired": bool(l.desired),
                        "idle_stop": self.idle_stop,
                        # 每条线都有自己的资料目录，面板要把路径显示出来，
                        # 用户才能确认「我登录的那个窗口」和「现在开的是不是同一份」。
                        "data_dir": str(udd),
                        "data_dir_exists": udd.is_dir(),
                        "profile": l.profile_directory,
                        "startup_urls": len(l.chrome.get("startup_urls") or []),
                        # 登录态告警（路径被改 / 谷歌 Cookie 变少）；空列表=正常
                        "login_warn": list(st.get("login_warn") or [])})
        return out


# ------------------------------------------------------------- 兜底入口
def serve_pac_only() -> int:
    """只提供 PAC 静态服务。

    用途：sysdefault_launch 在「管家没起来」时的兜底，避免用
    `pythonw -m http.server`（pythonw 下 sys.stderr 为 None 会崩，回空包）。
    """
    setup_logging()
    try:
        g = Guard()
    except Exception as e:  # noqa: BLE001
        log.error("PAC-ONLY 读取配置失败：%s", e)
        return 2
    if g.pac is None:
        log.error("config/profiles.json 里没有 pac_server 配置")
        return 3
    # ★ 必须先渲染：PAC 模板里是 __PORT_VIDEO__ / __PORT_DEFAULT__ 占位符，
    #   直接拿 assets/pac 起服务，浏览器会拿到一个没有 PROXY 语句的脚本。
    g.pac.directory = g._render_pac()
    if not g.pac.start():
        return 4
    log.info("PAC-ONLY 模式已启动 127.0.0.1:%s", g.pac.port)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0
