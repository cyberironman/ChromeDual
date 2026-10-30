# -*- coding: utf-8 -*-
"""一条代理线路 = 一个内核配置 + 一个端口 + **一份完全独立的 Chrome 资料目录**。

★ 本项目最重要的一条不变量：
  每条线路的 --user-data-dir 必须指向自己的 data\\chrome\\<线路>\\ 目录，
  绝不共用系统默认资料（%LOCALAPPDATA%\\Google\\Chrome\\User Data）。
  共用就等于「所有窗口共享同一套 Cookie / 书签 / 历史」，
  一个 Chrome 升级把库清掉、所有窗口一起掉线；一个窗口被搞坏，全部受影响。
  账户同步交给 Google 自己在线上做，本地只保证「每份数据各在各的文件夹」。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from app import login_backup, login_guard, paths
from app.config_store import read_startup_urls
from app.logging_setup import log
from app.net import (port_listeners, port_open, proc_label, probe_proxy,
                     uptime_text)
from app.procs import (chrome_running_with, kill_pids, norm_path,
                       pids_using_config)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class Line:
    def __init__(self, raw: dict, defaults: dict):
        self.id: str = raw["id"]
        self.name: str = raw["name"]
        self.enabled: bool = raw.get("enabled", True)
        self.http_port: int = int(raw.get("http_port") or 0)

        # 线路自己的「数据」目录：订阅、节点池、探测缓存、构建脚本
        self.source: Path = Path(raw.get("source") or paths.source_dir(self.id))
        # 运行期配置：放在 var\runtime\<线路>\，不和数据混在一起
        cfg = str(raw.get("config") or "").strip()
        self.config: Path = (Path(cfg) if cfg else paths.runtime_dir(self.id) / "xray-runtime.json")
        if not self.config.is_absolute():
            self.config = paths.runtime_dir(self.id) / self.config

        self.build: dict | None = raw.get("build")
        self.chrome: dict = raw.get("chrome", {})
        self.xray: str = raw.get("xray") or defaults.get("xray") or str(paths.XRAY)
        self.use_pac: bool = bool(self.chrome.get("pac"))
        self._proc: subprocess.Popen | None = None
        # 接管过来的内核 PID（上次管家退出时留下的孤儿，config 还是我们那份）
        self._adopted: set[int] = set()
        self._last_check: tuple[bool, str] = (False, "未检查")
        self._miss = 0
        self._next_retry = 0.0
        self._backoff = 60.0
        self._started_at = 0.0

        # ★ 按需启动（2026-10-30 起）：内核只在「这条线要用」时才跑。
        #   desired=False 的线路，保活线程**碰都不碰** —— 不启动、不重拉、不探测。
        #   用户开窗时置 True，窗口全关且超过 idle_stop 秒后自动置回 False 并停内核。
        self.desired = False
        self.last_window_seen = 0.0
        self.opened_at = 0.0

        # 文字警告：「资料目录被改过」「谷歌登录 Cookie 少了」这类。
        # 只存文字，面板原样显示；不会阻止开窗，但一定会被记录+显示。
        self.login_warn: list[str] = []

    # ------------------------------------------------------------- 资料目录
    @property
    def user_data_dir(self) -> Path:
        """这条线**独占**的资料目录。没配也绝不回落到系统默认目录。"""
        raw = str(self.chrome.get("user_data_dir") or "").strip()
        if raw:
            return Path(raw)
        return paths.chrome_dir(self.id)

    @property
    def profile_directory(self) -> str:
        return str(self.chrome.get("profile_directory") or "Default")

    # ------------------------------------------------------------- 端口分配
    @property
    def socks_port(self) -> int:
        """SOCKS 入口固定跟着 HTTP 入口走（+1），沿用原设计，一起随机。"""
        return self.http_port + 1 if self.http_port else 0

    def set_http_port(self, port: int) -> None:
        """由 `app/ports.py` 分配后回填。改完记得 sync_config_ports()。"""
        p = int(port or 0)
        if p == self.http_port:
            return
        old, self.http_port = self.http_port, p
        # profiles.json 里写 0（= 自动），所以 old 为 0 是常态，别拿它当"改端口"刷日志
        log.debug("[%s] 端口 %s → %s（socks %s）", self.name, old or "自动",
                  self.http_port, self.socks_port or "-")

    def sync_config_ports(self) -> None:
        """把内核配置里 http-in / socks-in 的端口改成当前分配值。

        端口是随机分配的，配置文件里却可能是上次随机的结果（甚至是写死的 10888），
        两边对不上就会出现「内核起来了但端口没就绪」——所以每次起内核前必须对齐。
        """
        if not self.config.is_file() or not self.http_port:
            return
        try:
            data = json.loads(self.config.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 内核配置解析失败，端口未同步：%s", self.name, e)
            return
        changed = False
        for ib in data.get("inbounds") or []:
            tag = str(ib.get("tag") or "")
            proto = str(ib.get("protocol") or "")
            if proto == "http" or tag.startswith("http"):
                want = self.http_port
            elif proto == "socks" or tag.startswith("socks"):
                want = self.socks_port
            else:
                continue
            if want and int(ib.get("port") or 0) != want:
                ib["port"] = want
                changed = True
        if not changed:
            return
        try:
            self.config.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            log.info("[%s] 内核配置端口已对齐 http=%s socks=%s",
                     self.name, self.http_port, self.socks_port)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 写内核配置失败：%s", self.name, e)

    # ----------------------------------------------------------------- 状态
    def port_up(self) -> bool:
        return port_open(self.http_port)

    def kernel_alive(self) -> bool:
        for pid in self.managed_pids():
            try:
                import psutil
                if psutil.Process(pid).is_running():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _holder(self, port: int) -> tuple[str, list[int]]:
        """看端口监听者是不是「本线路的内核」。

        返回 "mine" / "foreign" / "none" + 属于本线路的 pid 列表。
        """
        listeners = port_listeners(port)
        if not listeners:
            return "none", []
        target = norm_path(self.config)
        mine: list[int] = []
        for pid, cmd in listeners:
            c = norm_path(cmd)
            if target and target in c and ("xray" in c or "sing-box" in c):
                mine.append(pid)
        return ("mine" if mine else "foreign"), mine

    def owned_pids(self) -> list[int]:
        """**当前这个管家进程自己掌握的内核 PID**（自己 Popen 的 + 明确接管过的）。

        与 managed_pids() 的区别：那个只按 config 路径匹配，连上次退出遗留的
        孤儿也算 —— 那正是「四条线全绿其实只有一条在管」的来源。
        """
        out: list[int] = []
        if self._proc is not None and self._proc.poll() is None:
            out.append(self._proc.pid)
        for pid in list(self._adopted):
            try:
                import psutil
                if psutil.Process(pid).is_running():
                    out.append(pid)
                else:
                    self._adopted.discard(pid)
            except Exception:  # noqa: BLE001
                self._adopted.discard(pid)
        return out

    def adopt(self, pids: list[int]) -> None:
        """把遗留内核收编到本管家名下（config 相同，不必重启、不断网）。"""
        for pid in pids:
            if self._proc is not None and pid == self._proc.pid:
                continue
            self._adopted.add(pid)
            log.info("[%s] 已接管遗留内核 PID=%s", self.name, pid)

    def status(self) -> dict:
        """对外统一入口：在内核状态上补一条「登录态告警」（如果有）。"""
        st = self._status_raw()
        warn = getattr(self, "login_warn", None)
        if warn:
            st["login_warn"] = list(warn)
        return st

    def _status_raw(self) -> dict:
        """五态：running / orphan / foreign / starting / stopped。

        以前只有 port_open() 一个判断，于是「端口通」就被当成「运行中」，
        历史遗留的孤儿内核、手起的 http.server 全被算成自家线路，
        面板四条线全绿，用户完全分不清谁真在跑。
        """
        if self.use_pac:
            port = int(self.chrome.get("pac_port", 0) or 0)
            listeners = port_listeners(port) if port else []
            if not listeners:
                return {"state": "stopped", "ok": False, "pids": [], "port": port,
                        "detail": f"PAC 服务未启动（端口 {port}）"}
            pids = [pid for pid, _ in listeners if pid]
            if os.getpid() in pids:
                return {"state": "running", "ok": True, "pids": pids, "port": port,
                        "detail": f"PAC 服务运行中（端口 {port}，本程序内置）"}
            names = ", ".join(proc_label(pid) for pid, _ in listeners)
            return {"state": "foreign", "ok": True, "pids": pids, "port": port,
                    "detail": f"端口 {port} 由外部 PAC 服务提供：{names}（能用，但不是本程序起的）"}

        holder, mine = self._holder(self.http_port)
        if holder == "mine":
            owned = set(self.owned_pids())
            stray = [p for p in mine if p not in owned]
            up = uptime_text(mine[0]) if mine else ""
            up_txt = f" · 已运行 {up}" if up else ""
            is_adopted = any(p in self._adopted for p in mine)
            src = " · 接管自上次运行" if is_adopted else ""
            if not stray:
                return {"state": "running", "ok": True, "pids": mine, "port": self.http_port,
                        "detail": (f"内核运行中 · PID {', '.join(map(str, mine))}"
                                   f" · 端口 {self.http_port}{up_txt}{src}")}
            return {"state": "orphan", "ok": True, "pids": mine, "port": self.http_port,
                    "detail": (f"遗留内核 PID {', '.join(map(str, stray))} 仍在服务{up_txt}，"
                               f"但不归当前管家管（上次退出没清理）")}
        if holder == "foreign":
            names = ", ".join(proc_label(pid) for pid, _ in port_listeners(self.http_port))
            return {"state": "foreign", "ok": False, "pids": [], "port": self.http_port,
                    "detail": f"端口 {self.http_port} 被其他程序占用：{names}"}
        if self.kernel_alive():
            return {"state": "starting", "ok": False, "pids": self.managed_pids(),
                    "port": self.http_port,
                    "detail": f"内核进程在，端口 {self.http_port} 还没就绪"}
        return {"state": "stopped", "ok": False, "pids": [], "port": self.http_port,
                "detail": f"未启动（端口 {self.http_port} 空闲）"}

    def windows(self) -> list[int]:
        """用这套资料的 Chrome 主进程 PID（3 秒缓存，保活线程每轮都会问）。"""
        try:
            from app.procs import chrome_instances
            return chrome_instances().get(norm_path(self.user_data_dir), [])
        except Exception:  # noqa: BLE001
            return []

    def managed_pids(self) -> list[int]:
        return pids_using_config(self.config) if self.config.exists() else []

    def health(self, url: str) -> bool:
        ok, code, cost = probe_proxy(self.http_port, url)
        self._last_check = (ok, f"{code} {cost:.1f}s" if code else "超时")
        return ok

    @property
    def last_check_text(self) -> str:
        return self._last_check[1]

    # ------------------------------------------------------------- 生命周期
    def _normalize_config(self) -> None:
        """把内核配置里的日志路径统一指到 var\\logs\\<线路>-core.log。

        xray 的 log.error 是写在配置里的绝对路径，以前三条线各写各的
        （D:\\ChromeDual\\xray-core.log、D:\\ChromeDual\\us-core.log、Love\\xray-core.log），
        排障时根本不知道该看哪个文件。
        """
        if not self.config.is_file():
            return
        try:
            data = json.loads(self.config.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 内核配置解析失败，跳过日志归一：%s", self.name, e)
            return
        want = str(paths.line_core_log(self.id))
        cur = (data.get("log") or {}).get("error")
        if isinstance(cur, str) and norm_path(cur) == norm_path(want):
            return
        data.setdefault("log", {})["error"] = want
        try:
            self.config.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            log.info("[%s] 内核日志已指向 %s", self.name, want)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 写内核配置失败：%s", self.name, e)

    def _run_build(self) -> bool:
        if not self.build:
            return True
        script = Path(self.build.get("script") or "")
        if not script.is_absolute():
            cand = paths.TOOLS / script
            script = cand if cand.is_file() else self.source / script
        if not script.exists():
            log.error("[%s] 配置生成脚本不存在：%s", self.name, script)
            return False
        py = self.build.get("python") or sys.executable
        args = [str(py), str(script), "--out", str(self.config)]
        for k, v in self.build.get("args", {}).items():
            # 端口是随机分配的：配置里写死的 http-port / socks-port 必须换成当前值，
            # 否则生成出来的配置会绑回老端口（10898 那一批）。
            if k in ("http-port", "http_port") and self.http_port:
                v = self.http_port
            elif k in ("socks-port", "socks_port") and self.http_port:
                v = self.socks_port
            args += [f"--{k}", str(v)]
        log.info("[%s] 生成配置：%s", self.name, " ".join(args))
        try:
            p = subprocess.run(args, cwd=str(script.parent), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=300,
                               creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            log.error("[%s] 配置生成超时", self.name)
            return False
        if p.returncode != 0:
            log.error("[%s] 配置生成失败(%s)：%s", self.name, p.returncode,
                      (p.stderr or "").strip()[-1500:])
            return False
        if p.stderr:
            log.debug("[%s] build err: %s", self.name, p.stderr.strip()[-800:])
        self._normalize_config()
        return True

    def start(self, rebuild: bool = False) -> bool:
        if not self.enabled:
            return False
        if self.use_pac:
            return True            # PAC 线路不需要内核

        # 1) 端口已被**本线路自己的内核**监听 → 复用，不重建。
        #    不能只看「端口通」：孤儿内核 / 别的程序占着端口时盲目复用，
        #    等于把别人的进程当成自己的，之后状态全错。
        if not rebuild:
            holder, mine = self._holder(self.http_port)
            if holder == "mine":
                self.adopt(mine)
                self._started_at = time.time()
                log.info("[%s] 端口 %s 已由本线路内核(PID %s)监听，直接复用",
                         self.name, self.http_port, ", ".join(map(str, mine)))
                return True
            if holder == "foreign":
                names = ", ".join(proc_label(pid) for pid, _ in port_listeners(self.http_port))
                log.warning("[%s] 端口 %s 被其他程序占用，不抢占：%s",
                            self.name, self.http_port, names)
                return False

        # 2) 需要重建：先清掉所有「用本 config」的旧内核（含孤儿）
        stale = self.managed_pids()
        if stale:
            log.info("[%s] 清理旧内核 PID=%s", self.name, stale)
            kill_pids(stale)
            for _ in range(20):
                if not port_open(self.http_port):
                    break
                time.sleep(0.15)

        if rebuild and not self._run_build():
            return False
        if not self.config.exists() and self.build:
            if not self._run_build():      # 首次运行：配置还没生成过
                # 构建失败最常见的原因是「还没填订阅地址」。
                # 这时退回占位配置，软件照样能起来，缺什么由日志说清楚。
                log.warning("[%s] 自动构建配置失败，先用占位配置顶上（不是真代理）",
                            self.name)
                from app import bootstrap
                if not bootstrap.ensure_placeholder(self):
                    return False
        if not self.config.exists():
            # 没有任何配置：先生成一份「占位直连」配置，让软件能起来、窗口能开
            # （日志会明确提示这不是真代理，以及怎么换成真代理）。
            from app import bootstrap
            if not bootstrap.ensure_placeholder(self):
                log.error("[%s] 配置文件不存在：%s", self.name, self.config)
                return False
        self._normalize_config()
        self.sync_config_ports()      # 随机端口 → 内核配置，两边必须一致

        # 3) 起内核
        xray = Path(self.xray)
        if not xray.exists():
            log.error("[%s] 找不到内核：%s", self.name, xray)
            return False
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        log_path = paths.line_log(self.id)
        with open(log_path, "ab") as lf:
            self._proc = subprocess.Popen(
                [str(xray), "run", "-c", str(self.config)],
                cwd=str(xray.parent), stdout=lf, stderr=lf,
                creationflags=CREATE_NO_WINDOW)
        log.info("[%s] 内核已启动 PID=%s 端口=%s", self.name, self._proc.pid, self.http_port)

        # 4) 等端口
        for _ in range(80):
            time.sleep(0.25)
            if self._proc.poll() is not None:
                log.error("[%s] 内核启动即退出，看日志 %s", self.name, log_path)
                return False
            if port_open(self.http_port):
                log.info("[%s] 端口 %s 就绪", self.name, self.http_port)
                self._started_at = time.time()
                return True
        log.error("[%s] 20 秒内端口 %s 未就绪", self.name, self.http_port)
        return False

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None
        kill_pids(self.managed_pids())
        self._adopted.clear()
        self.desired = False
        self.last_window_seen = 0.0
        log.info("[%s] 已停止", self.name)

    # --------------------------------------------------------- 打开 Chrome
    def build_chrome_args(self, chrome_exe: str | None = None, url: str | None = None,
                          new_window: bool = True) -> list[str]:
        """拼 Chrome 启动参数（单独抽出来，方便有改动时先打印核对，不用真开窗口）。

        new_window=False 时不加 `--new-window`：窗口已开着就在里面开新标签页。
        点链接（系统默认浏览器转接器）必须走这种 —— 否则每点一个链接
        就弹一个新窗口，很快就满屏了。
        """
        args = [chrome_exe or str(paths.CHROME_EXE), "--hide-crash-restore-bubble"]

        # ★ 一定带 --user-data-dir：每条线一份独立资料，不做「空着就用系统默认」
        udd = self.user_data_dir
        args.append(f"--user-data-dir={udd}")
        args.append(f"--profile-directory={self.profile_directory}")

        if self.use_pac:
            args.append(f"--proxy-pac-url=http://127.0.0.1:{self.chrome['pac_port']}"
                        f"/{Path(self.chrome['pac']).name}")
        else:
            args.append(f"--proxy-server=http://127.0.0.1:{self.http_port}")
        # ⚠ extra_args 里四条线都写着 --new-window（见 config/profiles.json）。
        #   点链接要求「在当前窗口开新标签」时，必须把它滤掉 ——
        #   否则开关参数是摆设：每点一次链接就弹一个新窗口。
        extra = list(self.chrome.get("extra_args", []))
        if not new_window:
            extra = [a for a in extra if str(a).strip() != "--new-window"]
        args += extra

        if url:
            if new_window and "--new-window" not in args:
                args.append("--new-window")
            args.append(url)
            return args

        # 启动页优先用 config/profiles.json 里写死的 startup_urls。
        #
        # 踩过的坑（2026-10-30 实测）：Chrome 对 session.startup_urls、account_info
        # 这类首选项做了 HMAC 保护（校验值挂在 Secure Preferences 的 protection 段）。
        # 外部程序改 Preferences 文件，Chrome 认得出，会在启动/退出时**整段还原**，
        # 看起来就像「设置自己丢了」。所以起始页必须存在管家自己的配置里。
        has_url = any(str(a).lower().startswith(("http://", "https://", "file://"))
                      for a in args)
        if not has_url and self.chrome.get("use_startup_urls", True):
            urls = self.chrome.get("startup_urls") or \
                read_startup_urls(udd, self.profile_directory)
            if urls:
                if "--new-window" not in args:
                    args.append("--new-window")
                args += urls
                src = "配置" if self.chrome.get("startup_urls") else "Chrome 设置"
                log.info("[%s] 带上启动页 %d 个（来源：%s）", self.name, len(urls), src)
        return args

    def open_chrome(self, chrome_exe: str | None = None, extra_args: list[str] | None = None,
                    url: str | None = None, new_window: bool = True) -> str:
        """返回一句人类可读的结果说明。"""
        # 一开窗口就进入「按需存活」状态：保活线程从这一刻起才看顾这条线
        self.desired = True
        self.opened_at = time.time()
        self.last_window_seen = time.time()

        udd = self.user_data_dir
        udd.mkdir(parents=True, exist_ok=True)
        args = self.build_chrome_args(chrome_exe, url=url, new_window=new_window)
        if extra_args:
            args += extra_args

        running = chrome_running_with(udd)

        # 开窗**之前**先量一次登录态：路径有没有被换、Cookie 有没有少。
        # 这一步是只读的（Chrome 开着也能算），目的是让「没登录」这件事
        # 有明确的时间归属 —— 是这次开窗造成的，还是之前就已经少了。
        self.login_warn = []
        try:
            self.login_warn = login_guard.before_open(
                self.id, udd, self.profile_directory, name=self.name)
            for w in self.login_warn:
                log.warning("[%s] %s", self.name, w.replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 登录态体检跳过：%s", self.name, e)

        # 开窗口前把登录态（Cookies + Local State 成对）备份一份：
        # Chrome 升级或密钥对不上时会静默清库，没有备份就再也找不回来。
        # 只在没在跑的时候做 —— 跑着的时候 Cookies 被独占锁住，拷不动。
        if not running:
            try:
                bak = login_backup.snapshot(self.id, udd, self.profile_directory, tag=self.name)
                if bak:
                    log.info("[%s] 登录态已备份 -> %s", self.name, bak)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 登录态备份跳过：%s", self.name, e)

        if running and url:
            # 有 URL 时 Popen 不会被"丢弃"：Chrome 的新进程会把 URL 转交给
            # 已在跑的那个实例，然后自己退出。所以这里只是日志更好懂，不是错误。
            log.info("[%s] 窗口已开着(PID=%s)，把这条链接交给它打开", self.name, running)
        elif running:
            log.warning("[%s] 该资料目录已有 Chrome 在跑(PID=%s)，新参数会被忽略", self.name, running)
        subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
        log.info("[%s] 已启动 Chrome：%s", self.name, " ".join(args))
        if running and url:
            msg = f"{self.name}：窗口已开着，这条链接已交给它打开"
        elif running:
            msg = f"{self.name}：窗口本来就开着，参数不会生效（要点图标前先关掉那个窗口）"
        else:
            msg = f"{self.name}：已打开"
        if self.login_warn:
            msg += "\n⚠ " + "\n⚠ ".join(self.login_warn)
        return msg
