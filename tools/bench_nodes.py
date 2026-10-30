# -*- coding: utf-8 -*-
"""给一条线路订阅里的每个节点实测「可用性 + 下载吞吐」，产出排名。

为什么需要这个
--------------
这份订阅是 Cloudflare Pages(edgetunnel) 型代理：所有节点的 path/host/SNI 完全相同，
只差「入口 IP:端口」。出口都由同一个 Worker 决定，速度差异 100% 来自
「本机 → 入口 IP」这段公网链路。

原来的配置只挂 leastPing（延迟最低者优先）。但延迟低 ≠ 带宽好：公共优选 IP 里
大量节点 TCP 握手很快、下载却被挤到几百 KB/s，这正是「看着连上了但很慢」的原因。
实测更能说明问题：同一时刻 32 个节点只有 13 个 TCP 可连。

踩过的坑（务必保留）
--------------------
· 测速目标不能用 speed.cloudflare.com/__down：这条链路经 Worker 出去会直接 30s 超时，
  会把「节点好好的」误判成「0 KB/s」。改用 youtube.com 首页（约 950KB，贴近真实体验）。

用法
----
    python bench_nodes.py                   # 全量测（约 2-4 分钟）
    python bench_nodes.py --quick           # 只测延迟，跳过吞吐
    python bench_nodes.py --extra-ip 1.2.3.4 # 追加历史好 IP 一起测
结果写入 bench-result.json，并累积进 node-pool.json（历史 IP 质量记忆）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import build_balancer_config as bbc  # noqa: E402
from app import paths  # noqa: E402

#: 内核路径由 app.paths 统一决定（环境变量 CHROMEDUAL_XRAY 可覆盖），
#: 不写死任何人的安装位置 —— 以前这里硬编码过一个 v2rayN 目录，换台机器就跑不了。
XRAY = os.environ.get("CHROMEDUAL_XRAY") or str(paths.XRAY)
SUB = bbc.DEFAULT_SUB

# 测速目标：youtube 首页约 950KB，够代表真实浏览吞吐，且经 Worker 出去一定可达
SPEED_URL = "https://www.youtube.com/"
PING_URL = "https://www.gstatic.com/generate_204"
PING_TIMEOUT = 10
SPEED_TIMEOUT = 20
SPEED_ROUNDS = 2
SCREEN_WORKERS = 6      # 第一轮筛可用性时的并发数


# ------------------------------------------------------------------ 节点准备
def load_candidates(sub_url: str, extra: list, retries: int = 4) -> tuple[list[dict], str]:
    """extra 是 [(ip, port), ...]：历史池里已验证过的入口，需要跟新订阅一起测。"""
    uris: list[str] = []
    note = ""
    for attempt in range(retries):
        try:
            got = bbc.fetch_nodes(sub_url)
        except Exception as e:
            note = "第 %d 次拉取失败：%s" % (attempt + 1, e)
            print("      " + note)
            time.sleep(3)
            continue
        hosts = {urllib.parse.urlsplit(l).hostname for l in got}
        if len(got) >= 5 and len(hosts) >= 5:
            uris = got
            break
        note = "第 %d 次只拿到 %d 个节点" % (attempt + 1, len(got))
        print("      " + note + "，重试…")
        time.sleep(3)

    nodes: list[dict] = []
    seen: set[str] = set()
    counters: dict[str, int] = {}
    for uri in uris:
        u = urllib.parse.urlsplit(uri)
        remark = urllib.parse.unquote(u.fragment).strip()
        if any(k in remark for k in bbc.SKIP_KEYWORDS):
            continue
        host = u.hostname
        if not host or host in seen:
            continue
        seen.add(host)
        region = bbc.region_of(remark)
        counters[region] = counters.get(region, 0) + 1
        o = bbc.to_outbound(uri, "proxy-%s-%d" % (region, counters[region]))
        if not o:
            continue
        o["_port"] = int(u.port)
        o["_remark"] = remark
        o["_from"] = "sub"
        nodes.append(o)

    # 追加历史池里、本次订阅没给的 IP（同一 Worker，URI 参数可以照抄一个模板）
    if extra and nodes:
        tpl = uris[0]
        tpl_u = urllib.parse.urlsplit(tpl)
        tpl_q = tpl_u.query
        for entry in extra:
            ip, port = (entry if isinstance(entry, (tuple, list))
                        else (str(entry), 443))
            if ip in seen:
                continue
            seen.add(ip)
            rebuilt = urllib.parse.urlunsplit((
                tpl_u.scheme, "%s@%s:%d" % (tpl_u.username, ip, int(port)),
                tpl_u.path, tpl_q, "POOL-" + ip))
            o = bbc.to_outbound(rebuilt, "proxy-POOL-%d" % len(nodes))
            if o:
                o["_port"] = int(port)
                o["_remark"] = "POOL " + ip
                o["_from"] = "pool"
                nodes.append(o)
    return nodes, note


# ------------------------------------------------------------------ 延迟
def tcp_latency(host: str, port: int, tries: int = 3, timeout: float = 2.5) -> float | None:
    vals = []
    for _ in range(tries):
        t0 = time.perf_counter()
        try:
            s = socket.create_connection((host, port), timeout)
            s.close()
            vals.append((time.perf_counter() - t0) * 1000)
        except Exception:
            continue
        time.sleep(0.03)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def measure_latency(nodes: list[dict]) -> None:
    lock = threading.Lock()

    def one(n: dict) -> None:
        v = tcp_latency(n["_address"], n["_port"])
        with lock:
            n["_latency"] = v

    ts = [threading.Thread(target=one, args=(n,), daemon=True) for n in nodes]
    for t in ts:
        t.start()
    for t in ts:
        t.join(8)


# ------------------------------------------------------------------ 批量 HTTP
class MultiProxy:
    """一个 xray 进程，每节点一个 http 入口，按需发请求。"""

    def __init__(self, nodes: list[dict], tag_prefix: str = "b"):
        self.nodes = nodes
        self.prefix = tag_prefix
        self.proc = None
        self.tmp = None
        self.ports: list[int] = []

    def __enter__(self) -> "MultiProxy":
        if not self.nodes:
            return self
        ports, socks = bbc._reserve_free_ports(len(self.nodes))
        self.ports = ports
        inbounds, rules = [], []
        for i, n in enumerate(self.nodes):
            inbounds.append({"tag": "%s%d" % (self.prefix, i), "listen": "127.0.0.1",
                             "port": ports[i], "protocol": "http",
                             "settings": {"timeout": 0}})
            rules.append({"type": "field", "inboundTag": ["%s%d" % (self.prefix, i)],
                          "outboundTag": n["tag"]})
        cfg = {
            "log": {"loglevel": "error", "access": "none"},
            "inbounds": inbounds,
            "outbounds": [bbc._strip_private(n) for n in self.nodes] + [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "block", "protocol": "blackhole"},
            ],
            "routing": {"domainStrategy": "AsIs", "rules": rules + [
                {"type": "field", "outboundTag": "direct",
                 "ip": ["127.0.0.0/8", "10.0.0.0/8", "192.168.0.0/16"]},
            ]},
        }
        self.tmp = os.path.join(tempfile.gettempdir(), "love_bench_%d.json" % os.getpid())
        with open(self.tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        self.proc = subprocess.Popen([XRAY, "run", "-c", self.tmp],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for i in range(len(self.nodes)):
            for _ in range(60):
                try:
                    s = socket.create_connection(("127.0.0.1", ports[i]), 0.3)
                    s.close()
                    break
                except Exception:
                    time.sleep(0.2)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(4)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.tmp:
            try:
                os.remove(self.tmp)
            except Exception:
                pass

    def fetch(self, i: int, url: str, timeout: float) -> tuple[int, int, float]:
        """返回 (字节数, http 状态, 耗时秒)。失败返回 (0, 0, 耗时)。"""
        proxy = "http://127.0.0.1:%d" % self.ports[i]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": bbc.BROWSER_UA})
            with opener.open(req, timeout=timeout) as r:
                n = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    n += len(chunk)
                return n, r.status, time.perf_counter() - t0
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            return 0, e.code, time.perf_counter() - t0
        except Exception:
            return 0, 0, time.perf_counter() - t0


# ------------------------------------------------------------------ 阶段 1：筛可用
def screen_alive(nodes: list[dict]) -> None:
    """并发探 gstatic 204，不可用的直接标死。"""
    with MultiProxy(nodes, "s") as mp:
        lock = threading.Lock()
        idx = list(range(len(nodes)))
        queue = list(idx)

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    i = queue.pop(0)
                n = nodes[i]
                nb, code, dt = mp.fetch(i, PING_URL, PING_TIMEOUT)
                n["_alive"] = (code == 204)
                n["_ping_ms"] = int(dt * 1000) if code == 204 else None

        ts = [threading.Thread(target=worker, daemon=True) for _ in range(SCREEN_WORKERS)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(PING_TIMEOUT + 10)


# ------------------------------------------------------------------ 阶段 2：测吞吐
def measure_speed(nodes: list[dict]) -> None:
    """串行测 youtube 首页吞吐，避免互相抢带宽。"""
    with MultiProxy(nodes, "t") as mp:
        for i, n in enumerate(nodes):
            best = 0.0
            best_ms = None
            total = 0
            for _ in range(SPEED_ROUNDS):
                nb, code, dt = mp.fetch(i, SPEED_URL, SPEED_TIMEOUT)
                if nb <= 0:
                    continue
                kbps = nb / 1024.0 / max(dt, 0.001)
                if kbps > best:
                    best = kbps
                    best_ms = int(dt * 1000)
                    total = nb
            n["_kbps"] = round(best, 1)
            n["_bytes"] = total
            n["_speed_ms"] = best_ms
            print("  %-16s %-3s %8.1f KB/s  %s B  %s ms"
                  % (n["_address"], n["_region"], n["_kbps"], n["_bytes"],
                     best_ms if best_ms else "-"), flush=True)


# ------------------------------------------------------------------ 池记忆
def update_pool(path: str, rows: list[dict]) -> dict:
    pool: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                pool = json.load(f)
        except Exception:
            pool = {}
    entries = pool.setdefault("ips", {})
    now = time.time()
    for r in rows:
        e = entries.setdefault(r["address"], {"first_seen": now, "hits": 0,
                                             "best_kbps": 0, "fail": 0})
        e["hits"] = e.get("hits", 0) + 1
        e["last_seen"] = now
        e["region"] = r["region"]
        # 端口必须记：这类反代 IP 只在订阅给的那个端口上监听，重建 URI 时要原样带回去
        e["port"] = int(r.get("port") or 443)
        if r["kbps"] > 0:
            e["best_kbps"] = max(e.get("best_kbps", 0), r["kbps"])
            e["last_kbps"] = r["kbps"]
            e["last_ok"] = now
            e["fail"] = 0
        else:
            e["fail"] = e.get("fail", 0) + 1
    pool["stamp"] = now
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)
    return pool


# ---------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub-url", default=SUB)
    ap.add_argument("--out", default=os.path.join(HERE, "bench-result.json"))
    ap.add_argument("--pool", default=os.path.join(HERE, "node-pool.json"))
    ap.add_argument("--quick", action="store_true", help="只测延迟/可用性，不测吞吐")
    ap.add_argument("--use-pool", action="store_true", help="把历史池里的 IP 也一起测")
    ap.add_argument("--max-nodes", type=int, default=10, help="重建配置时最多保留节点数")
    ap.add_argument("--no-apply", action="store_true", help="只测速，不改配置")
    ap.add_argument("--restart", action="store_true", help="重建后立刻重启内核")
    a = ap.parse_args()

    extra: list[tuple[str, int]] = []
    if a.use_pool and os.path.exists(a.pool):
        try:
            with open(a.pool, encoding="utf-8") as f:
                p = json.load(f)
            for ip, e in (p.get("ips") or {}).items():
                if e.get("best_kbps", 0) > 0:
                    extra.append((ip, int(e.get("port") or 443)))
            extra = extra[:20]
        except Exception:
            pass

    print("[1/4] 拉订阅 …")
    nodes, note = load_candidates(a.sub_url, extra)
    if not nodes:
        print("FAIL 拿不到任何节点：%s" % note)
        return 1
    print("      候选节点 %d 个（含历史池 %d 个）" % (len(nodes), len(extra)))

    print("[2/4] 测 TCP 延迟（并发）…")
    measure_latency(nodes)
    alive = [n for n in nodes if n.get("_latency") is not None]
    print("      TCP 可连 %d / %d" % (len(alive), len(nodes)))

    print("[3/4] 经内核探活（并发的 generate_204）…")
    screen_alive(nodes)
    usable = [n for n in nodes if n.get("_alive")]
    usable.sort(key=lambda x: x.get("_ping_ms") or 99999)
    print("      真的能上网 %d / %d" % (len(usable), len(nodes)))
    for n in usable[:12]:
        print("        %-16s %-3s ping %4s ms" % (n["_address"], n["_region"], n.get("_ping_ms")))

    if not a.quick and usable:
        print("[4/4] 测下载吞吐（串行 youtube 首页，%d 轮取最好）…" % SPEED_ROUNDS)
        measure_speed(usable)
    else:
        for n in nodes:
            n.setdefault("_kbps", 0.0)

    rows = []
    for n in nodes:
        rows.append({
            "address": n["_address"],
            "port": n["_port"],
            "region": n["_region"],
            "remark": n.get("_remark", ""),
            "from": n.get("_from", "sub"),
            "latency_ms": round(n["_latency"], 1) if n.get("_latency") else None,
            "ping_ms": n.get("_ping_ms"),
            "kbps": n.get("_kbps", 0.0),
            "bytes": n.get("_bytes", 0),
            "alive": bool(n.get("_alive")),
        })
    rows.sort(key=lambda r: (-r["kbps"], r["ping_ms"] or 99999, r["latency_ms"] or 99999))

    print("\n=== 排名（按吞吐）===")
    for i, r in enumerate(rows, 1):
        mark = "OK " if r["alive"] else "×  "
        print("%2d. %s%-16s %-3s %8.1f KB/s  ping %5s ms  tcp %6s ms"
              % (i, mark, r["address"], r["region"], r["kbps"],
                 r["ping_ms"], r["latency_ms"]))

    good = [r for r in rows if r["kbps"] > 0]
    data = {
        "stamp": time.time(),
        "sub": a.sub_url,
        "total": len(nodes),
        "alive": len(usable),
        "best_kbps": good[0]["kbps"] if good else 0,
        "median_kbps": sorted(r["kbps"] for r in good)[len(good) // 2] if good else 0,
        "rows": rows,
        "top": [r["address"] for r in good[:12]],
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    pool = update_pool(a.pool, rows)
    print("\n已写入 %s（池内累计 %d 个 IP）" % (a.out, len(pool.get("ips") or {})))
    if good:
        print("最快 %s  %.1f KB/s   中位 %.1f KB/s   可用 %d/%d"
              % (good[0]["address"], good[0]["kbps"], data["median_kbps"],
                 len(usable), len(nodes)))

    # ---- 测完直接把结果落到配置里，否则「测了不用」等于白测
    if not a.no_apply:
        print("\n[5/5] 按测速结果重建配置 …")
        cfg = {}
        try:
            with open(os.path.join(HERE, "chrome-proxy.config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        # 端口没配就随机挑一对（以前是写死 10898/10899，会跟别的代理程序撞）
        http_port, socks_port = bbc.resolve_ports(
            int(cfg.get("httpPort") or 0), int(cfg.get("socksPort") or 0))
        xray = cfg.get("xrayPath") or XRAY
        try:
            path, meta = bbc.build(HERE, a.sub_url, http_port, socks_port,
                                   do_probe=True, xray=xray,
                                   do_screen=True, max_nodes=a.max_nodes)
            print("      已生成 %s  nodes=%d" % (path, meta["nodes"]))
        except Exception as e:
            print("      重建失败：%s" % e)
            return 1

        if a.restart:
            import psutil
            target = os.path.normcase(os.path.abspath(path))
            killed = 0
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if (p.info["name"] or "").lower() != "xray.exe":
                        continue
                    if target in os.path.normcase(" ".join(p.info["cmdline"] or [])):
                        p.kill()
                        killed += 1
                except Exception:
                    pass
            time.sleep(2)
            logf = open(os.path.join(HERE, "xray-core.log"), "w", encoding="utf-8")
            subprocess.Popen([xray, "run", "-c", path], cwd=os.path.dirname(xray),
                             stdout=logf, stderr=logf)
            okport = False
            for _ in range(60):
                try:
                    s = socket.create_connection(("127.0.0.1", http_port), 0.4)
                    s.close()
                    okport = True
                    break
                except Exception:
                    time.sleep(0.5)
            print("      已重启内核（停 %d 个），端口 %d %s"
                  % (killed, http_port, "就绪" if okport else "未就绪"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
