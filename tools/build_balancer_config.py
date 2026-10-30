# -*- coding: utf-8 -*-
"""为一条线路生成 xray 配置：订阅全部节点 + 自动择优 + 指定站点按国家出网。

三个层次
--------
1. 默认线路：全部节点都做成 outbound，observatory 主动探活，balancer 用 leastPing 挑最快的一条。
   为什么要这样：这份订阅按天轮换，手选某个国家必然踩到慢节点（实测单节点 DE 打开 google 9.9 秒）。

2. 指定站点走指定国家：某些站点有 AWS WAF 地域/机房风控，
   走欧洲节点会返回 `202 + x-amzn-waf-action: challenge`、正文为空 → Chrome 里就是一片白。
   这类站点在 region-rules.json 里登记，单独走该国节点的专用 balancer。

3. 该国节点里「实测能打开该站点」的优先：WAF 判定按出口 IP 走，同一国家的节点有的通有的不通。
   构建时用一次性的并发探测（单个 xray 进程 + 每节点一个 http 入口）挑出能返回正文的节点，
   结果缓存 6 小时（缓存文件 region-probe.json），避免每次启动都重探。

用法：
    python build_balancer_config.py
    python build_balancer_config.py --root D:\\ChromeDual\\Love --http-port 23456 --socks-port 23457
    python build_balancer_config.py --no-probe        # 跳过探测，直接用该国全部节点
退出码 0 = 生成成功。

端口：`--http-port` 不给（或给 0）就**随机挑一对空闲端口**。以前默认写死
10898/10899，跟 v2rayN、另两个代理管家一起挤在 108xx 段，谁先绑谁活；
绑不上的那个表现成「内核起来了但端口没就绪」，日志里看不出是端口冲突。
随机段 20000-39998 与「分应用代理管家」（10000-19998）、「Codex 管家」
（40000-59998）完全不重叠。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

#: 随机端口区间（含左不含右，偶数下界保证 http/socks 成对不越界）。
#: 与 app/ports.py 的 RANGE_START/RANGE_END 同一段，改一处记得改另一处。
PORT_LO, PORT_HI = 20000, 39998


def _port_free(port: int) -> bool:
    """127.0.0.1:port 现在能不能绑上。不能设 SO_REUSEADDR —— 会让「已被监听」也绑成功。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def pick_port_pair(tries: int = 300) -> tuple[int, int]:
    """随机挑一对相邻的空闲端口 (http, socks)。挑不到抛 RuntimeError。"""
    rnd = random.SystemRandom()
    for _ in range(tries):
        p = rnd.randrange(PORT_LO, PORT_HI, 2)
        if _port_free(p) and _port_free(p + 1):
            return p, p + 1
    for p in range(PORT_LO, PORT_HI, 2):
        if _port_free(p) and _port_free(p + 1):
            return p, p + 1
    raise RuntimeError(f"找不到成对空闲端口（{PORT_LO}-{PORT_HI} 都被占了？）")


def resolve_ports(http_port: int, socks_port: int) -> tuple[int, int]:
    """把命令行/配置里的端口解释成一对实际可用的端口。

    http_port <= 0 → 随机；socks_port <= 0 → 用 http_port + 1。
    """
    if int(http_port or 0) > 0:
        h = int(http_port)
        s = int(socks_port) if int(socks_port or 0) > 0 else h + 1
        return h, s
    return pick_port_pair()

# 订阅地址**不写死在代码里** —— 它带 token，属于个人信息，绝不能进仓库。
# 用法二选一：
#   1) 命令行传：--sub-url "https://…/sub?token=…"
#   2) 放进 <root>/chrome-proxy.config.json 的 subscriptionUrl 字段
DEFAULT_SUB = ""
# 订阅里混着「加入我的频道」这类广告节点，不能当真实节点用
SKIP_KEYWORDS = ("t.me", "telegram", "加入我的频道", "解锁更多")
UA = "v2rayN/7.24.3"
# 探活用浏览器 UA，避免 WAF 因为 UA 是脚本而直接挑战
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
PROBE_TTL = 6 * 3600            # 探测结果缓存 6 小时
PROBE_BASE_PORT = 10960         # 一次性探测用的临时端口段
PROBE_MIN_BODY = 5000           # 正文小于这个字节数视为被拦截
# 主线路只挂「实测最快」的前 N 个节点。取 3 是折中：既有速度、又留了两个备胎
# （leastPing 会在 observatory 判定健康的节点里挑，单点挂掉不影响）。
FAST_N = 3


# --------------------------------------------------------------------------- 订阅
def fetch_nodes(url: str, timeout: int = 25) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout).read().strip()
    if b"vless://" in raw:
        text = raw.decode("utf-8", "ignore")
    else:
        b = raw.replace(b"-", b"+").replace(b"_", b"/")
        b += b"=" * ((4 - len(b) % 4) % 4)
        text = base64.b64decode(b).decode("utf-8", "ignore")
    return [l.strip() for l in text.splitlines() if l.strip().startswith("vless://")]


def region_of(remark: str) -> str:
    """节点备注形如 'US' / 'US 2' / '🇺🇸US-LA'，取国家码；取不到就归到 XX。"""
    m = re.match(r"^[^A-Za-z]*([A-Z]{2})\b", remark or "")
    return m.group(1) if m else "XX"


def to_outbound(uri: str, tag: str) -> dict | None:
    u = urllib.parse.urlsplit(uri)
    if not u.hostname or not u.port or not u.username:
        return None
    q = dict(urllib.parse.parse_qsl(u.query))
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": u.hostname,
                "port": int(u.port),
                "users": [{"id": u.username, "encryption": "none", "flow": ""}],
            }]
        },
        "streamSettings": {
            "network": q.get("type", "ws"),
            "security": q.get("security", "tls"),
            "tlsSettings": {
                "serverName": q.get("sni") or u.hostname,
                "fingerprint": q.get("fp", "chrome"),
                "allowInsecure": False,
            },
            "wsSettings": {
                "path": q.get("path", "/"),
                "headers": {"Host": q.get("host") or q.get("sni") or u.hostname},
            },
        },
        "_address": u.hostname,   # 内部用，写盘前删掉
        "_region": region_of(urllib.parse.unquote(u.fragment)),
    }


# ------------------------------------------------------------------- 一次性并发探测
def _reserve_free_ports(count: int) -> tuple[list[int], list]:
    """向系统申请 count 个空闲 TCP 端口（绑定到 0 由 OS 分配），返回 (端口列表, 占位 socket 列表)。

    为什么不用写死的 PROBE_BASE_PORT：
        本机有些常驻进程（如 WorkBuddy 自身走 10888 代理时）会占用 10960 附近的临时端口，
        写死端口会让「探测用的临时 xray」绑定失败 → 探测全失败 → 误以为 0 个节点可用。
        改为每次动态申请空闲端口，彻底避开冲突。
    """
    socks = []
    ports = []
    try:
        for _ in range(count):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            s.settimeout(1)
            ports.append(s.getsockname()[1])
            socks.append(s)
        return ports, socks
    except Exception:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        raise


def probe_working(nodes: list[dict], target: str, xray: str) -> set[str]:
    """返回能正常打开 target 的节点地址集合。

    做法：一个 xray 进程里每个节点配一个独立的 http 入口（端口动态申请，避开占用），
    按 inboundTag 直连到对应节点；多线程各自走自己的入口请求目标站。
    """
    usable = [n for n in nodes if n.get("_address")]
    if not usable:
        return set()
    usable = usable[:40]
    ports, socks = _reserve_free_ports(len(usable))
    inbounds, rules = [], []
    for i, n in enumerate(usable):
        inbounds.append({"tag": "in%d" % i, "listen": "127.0.0.1", "port": ports[i],
                         "protocol": "http", "settings": {"timeout": 0}})
        rules.append({"type": "field", "inboundTag": ["in%d" % i], "outboundTag": n["tag"]})

    cfg = {
        "log": {"loglevel": "error", "access": "none"},
        "inbounds": inbounds,
        "outbounds": [_strip_private(n) for n in usable] + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "AsIs", "rules": rules + [
            {"type": "field", "outboundTag": "direct",
             "ip": ["127.0.0.0/8", "10.0.0.0/8", "192.168.0.0/16"]},
        ]},
    }

    tmp = os.path.join(tempfile.gettempdir(), "love_probe_%d.json" % os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False)

    # 必须在启动 xray 前释放预留端口，否则 xray 绑定会失败（Address already in use）
    for s in socks:
        try:
            s.close()
        except Exception:
            pass
    socks = []

    proc = subprocess.Popen([xray, "run", "-c", tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok: set[str] = set()
    lock = threading.Lock()
    try:
        # 等全部入口监听
        for i in range(len(usable)):
            port = ports[i]
            for _ in range(60):
                try:
                    s = socket.create_connection(("127.0.0.1", port), 0.25)
                    s.close()
                    break
                except Exception:
                    time.sleep(0.2)

        def one(n: dict, port: int) -> None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": "http://127.0.0.1:%d" % port, "https": "http://127.0.0.1:%d" % port}))
            try:
                req = urllib.request.Request(target, headers={"User-Agent": BROWSER_UA})
                with opener.open(req, timeout=25) as r:
                    body = r.read()
                    waf = (r.headers.get("x-amzn-waf-action") or "").lower()
                if r.status == 200 and len(body) >= PROBE_MIN_BODY and waf != "challenge":
                    with lock:
                        ok.add(n["_address"])
            except Exception:
                pass

        threads = [threading.Thread(target=one, args=(n, ports[i]), daemon=True)
                   for i, n in enumerate(usable)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(35)
    finally:
        try:
            proc.terminate()
            proc.wait(4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(tmp)
        except Exception:
            pass
    return ok


def _strip_private(o: dict) -> dict:
    return {k: v for k, v in o.items() if not k.startswith("_")}


# --------------------------------------------------------------- 构建期探活筛选
def screen_alive(nodes: list[dict], xray: str,
                 target: str = "https://www.gstatic.com/generate_204",
                 timeout: int = 8, workers: int = 8) -> dict[str, int]:
    """并发经内核探活，返回 {address: 毫秒}，只含真正返回 204 的节点。

    为什么必须在构建期做：实测这份订阅 32 个节点里稳定有 12~19 个是死的
    （TCP 看似能连、TLS 握手直接超时）。死节点留在配置里，leastPing 会先花时间
    探测它们，偶尔还会被选中 → 浏览器表现为「转圈很久才出来」。
    """
    usable = [n for n in nodes if n.get("_address")]
    if not usable or not xray:
        return {}
    ports, socks = _reserve_free_ports(len(usable))
    inbounds, rules = [], []
    for i, n in enumerate(usable):
        inbounds.append({"tag": "s%d" % i, "listen": "127.0.0.1", "port": ports[i],
                         "protocol": "http", "settings": {"timeout": 0}})
        rules.append({"type": "field", "inboundTag": ["s%d" % i], "outboundTag": n["tag"]})

    cfg = {
        "log": {"loglevel": "error", "access": "none"},
        "inbounds": inbounds,
        "outbounds": [_strip_private(n) for n in usable] + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }
    tmp = os.path.join(tempfile.gettempdir(), "love_screen_%d.json" % os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False)
    for s in socks:
        try:
            s.close()
        except Exception:
            pass

    proc = subprocess.Popen([xray, "run", "-c", tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result: dict[str, int] = {}
    lock = threading.Lock()
    try:
        for i in range(len(usable)):
            for _ in range(50):
                try:
                    s = socket.create_connection(("127.0.0.1", ports[i]), 0.3)
                    s.close()
                    break
                except Exception:
                    time.sleep(0.2)

        queue = list(range(len(usable)))

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    i = queue.pop(0)
                proxy = "http://127.0.0.1:%d" % ports[i]
                opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                    {"http": proxy, "https": proxy}))
                t0 = time.perf_counter()
                try:
                    req = urllib.request.Request(target, headers={"User-Agent": BROWSER_UA})
                    with opener.open(req, timeout=timeout) as r:
                        r.read(16)
                        code = r.status
                    if code in (200, 204):
                        ms = int((time.perf_counter() - t0) * 1000)
                        with lock:
                            result[usable[i]["_address"]] = ms
                except Exception:
                    pass

        ts = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout + 8)
    finally:
        try:
            proc.terminate()
            proc.wait(4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(tmp)
        except Exception:
            pass
    return result


def probe_throughput(nodes: list[dict], xray: str, top_n: int = 10,
                     target: str = "https://www.youtube.com/",
                     timeout: int = 14) -> dict[str, float]:
    """对候选节点串行实测下载吞吐，返回 {address: KB/s}。

    为什么构建时必须现场测
    ----------------------
    这类公共反代 IP 的带宽波动极大：实测同一个韩国节点 30 分钟内从 640 KB/s
    掉到 230 KB/s，仅凭历史记录排序必然选错。只有「此刻」的数字才有意义。

    只测 top_n 个：先按 ping 升序取前 top_n（ping 与吞吐有明显相关性，
    实测 ping 4s 的节点吞吐只有 ping 2.2s 节点的一半），再串行逐个下载
    youtube 首页。串行是故意的 —— 并发会互相抢带宽，测出来没法比较。
    """
    if not nodes or not xray:
        return {}
    cand = nodes[:top_n]
    ports, socks = _reserve_free_ports(len(cand))
    inbounds, rules = [], []
    for i, n in enumerate(cand):
        inbounds.append({"tag": "p%d" % i, "listen": "127.0.0.1", "port": ports[i],
                         "protocol": "http", "settings": {"timeout": 0}})
        rules.append({"type": "field", "inboundTag": ["p%d" % i], "outboundTag": n["tag"]})

    cfg = {
        "log": {"loglevel": "error", "access": "none"},
        "inbounds": inbounds,
        "outbounds": [_strip_private(n) for n in cand] + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }
    tmp = os.path.join(tempfile.gettempdir(), "love_speed_%d.json" % os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False)
    for s in socks:
        try:
            s.close()
        except Exception:
            pass

    proc = subprocess.Popen([xray, "run", "-c", tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out: dict[str, float] = {}
    try:
        for i in range(len(cand)):
            for _ in range(50):
                try:
                    s = socket.create_connection(("127.0.0.1", ports[i]), 0.3)
                    s.close()
                    break
                except Exception:
                    time.sleep(0.2)
        for i, n in enumerate(cand):
            proxy = "http://127.0.0.1:%d" % ports[i]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            t0 = time.perf_counter()
            try:
                req = urllib.request.Request(target, headers={"User-Agent": BROWSER_UA})
                with opener.open(req, timeout=timeout) as r:
                    nbytes = 0
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        nbytes += len(chunk)
                if nbytes > 0:
                    dt = max(time.perf_counter() - t0, 0.001)
                    out[n["_address"]] = nbytes / 1024.0 / dt
            except Exception:
                pass
    finally:
        try:
            proc.terminate()
            proc.wait(4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(tmp)
        except Exception:
            pass
    return out


def load_bench(root: str, ttl: int = 12 * 3600) -> dict[str, float]:
    """读取 bench-result.json 的吞吐排名 {address: KB/s}，过期或不存在就返回空。"""
    path = os.path.join(root, "bench-result.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - float(d.get("stamp", 0)) > ttl:
            return {}
        return {r["address"]: float(r.get("kbps") or 0)
                for r in d.get("rows", []) if r.get("address")}
    except Exception:
        return {}


def load_pool_entries(root: str, limit: int = 24) -> list[tuple[str, int, str]]:
    """从 node-pool.json 取「曾经实测有吞吐」的 (ip, port, region)，按历史最好速度降序。

    为什么必须这么做
    ----------------
    这份订阅每拉一次就换一整批入口 IP（实测 32 个里只有 1 个与上次重合）。
    如果只用当次订阅的节点，那 bench 测出来的「哪几个快」下次构建就全作废了。
    把验证过的好 IP 存进池子、构建时用订阅里的最新 URI 模板重建，就能保证
    「每次都能用回已知快的那几个」，而不是每次重新赌一批盲盒。
    """
    path = os.path.join(root, "node-pool.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            pool = json.load(f)
    except Exception:
        return []
    items = []
    for ip, e in (pool.get("ips") or {}).items():
        if not isinstance(e, dict):
            continue
        kbps = float(e.get("best_kbps") or 0)
        port = int(e.get("port") or 443)
        region = str(e.get("region") or "XX").upper()
        if kbps <= 0:
            continue
        items.append((kbps, ip, port, region))
    items.sort(reverse=True)
    return [(ip, port, region) for _k, ip, port, region in items[:limit]]


def rebuild_uri_from_template(tpl_uri: str, ip: str, port: int) -> str:
    """用订阅里的最新 URI 当模板（保证 UUID/path/SNI 都是最新的），只替换入口 IP:端口。"""
    u = urllib.parse.urlsplit(tpl_uri)
    netloc = "%s@%s:%d" % (u.username, ip, port)
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, "POOL-" + ip))




# ------------------------------------------------------------------------- 规则文件
def load_region_rules(root: str) -> list[dict]:
    """region-rules.json 形如：
    [{"region": "US", "domains": ["example.com"], "probe": "https://www.example.com/"}]
    """
    path = os.path.join(root, "region-rules.json")
    if not os.path.exists(path):
        return []
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for r in data:
            region = str(r.get("region", "")).upper()
            domains = [d.strip() for d in r.get("domains", []) if d and d.strip()]
            if not region or not domains:
                continue
            probe = r.get("probe") or ("https://www.%s/" % domains[0].lstrip("*."))
            out.append({"region": region, "domains": domains, "probe": probe})
        return out
    except Exception as e:
        print("WARN region-rules.json 解析失败：%s" % e, file=sys.stderr)
        return []


def cache_key(sub_url: str, region: str) -> str:
    h = hashlib.sha1((sub_url + "|" + region).encode("utf-8")).hexdigest()[:10]
    return "sub-%s-%s" % (h, region)


def read_cache(root: str) -> dict:
    path = os.path.join(root, "region-probe.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_cache(root: str, data: dict) -> None:
    path = os.path.join(root, "region-probe.json")
    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ------------------------------------------------------------------------- 主构建
def build(root: str, sub_url: str, http_port: int, socks_port: int,
          do_probe: bool = True, xray: str = "", do_screen: bool = True,
          max_nodes: int = 10, do_speed: bool = True,
          out: str = "") -> tuple[str, dict]:
    """生成 xray 配置。

    root 只是「订阅 / 节点池 / 探测缓存」所在的目录（数据）；
    out 是生成的配置文件路径（运行期文件），不传就落在 root 下，兼容旧调用。
    """
    uris = fetch_nodes(sub_url)
    if not uris:
        raise RuntimeError("no vless node in subscription, check sub url")

    outs: list[dict] = []
    used: set[str] = set()
    counters: dict[str, int] = {}
    for uri in uris:
        u = urllib.parse.urlsplit(uri)
        remark = urllib.parse.unquote(u.fragment).strip()
        if any(k in remark for k in SKIP_KEYWORDS):
            continue
        region = region_of(remark)
        counters[region] = counters.get(region, 0) + 1
        tag = "proxy-%s-%d" % (region, counters[region])
        n = 2
        while tag in used:
            tag = "proxy-%s-%d-%d" % (region, counters[region], n)
            n += 1
        used.add(tag)
        o = to_outbound(uri, tag)
        if o:
            outs.append(o)
    if not outs:
        raise RuntimeError("all nodes filtered out")

    # ---- 0) 补回池里「曾经实测够快」的入口 IP
    # 订阅每次换一整批 IP，不补的话每次重建都等于重新赌盲盒，
    # bench 辛苦测出来的「哪几个快」下一次就全作废。
    have = {o["_address"] for o in outs}
    tpl = next((u for u in uris
                if not any(k in urllib.parse.unquote(u) for k in SKIP_KEYWORDS)), uris[0])
    added = 0
    for ip, port, region in load_pool_entries(root):
        if ip in have:
            continue
        o = to_outbound(rebuild_uri_from_template(tpl, ip, port),
                        "proxy-POOL-%d" % (added + 1))
        if not o:
            continue
        o["_region"] = region if region and region != "XX" else o["_region"]
        o["_from"] = "pool"
        outs.append(o)
        have.add(ip)
        added += 1
    if added:
        print("INFO 从历史池补回 %d 个已验证入口 IP（本次订阅没给）" % added,
              file=sys.stderr)

    rules_for_region = load_region_rules(root)

    # ---- 1) 构建期探活：剔除死节点
    # 这份订阅常年有 1/3 到 1/2 的节点是死的（TCP 能连、TLS 握手超时）。
    # 死节点留在配置里会让 leastPing 白花时间探测，偶尔还会被选中 → 浏览器长时间转圈。
    total_raw = len(outs)
    if xray and do_screen:
        alive = screen_alive(outs, xray)
        if len(alive) >= 3:
            for o in outs:
                o["_ping"] = alive.get(o["_address"])
            outs = [o for o in outs if o["_address"] in alive]
            print("INFO 探活：%d/%d 个节点真的能用，已剔除死节点"
                  % (len(outs), total_raw), file=sys.stderr)
        else:
            print("WARN 探活只拿到 %d 个可用节点，判定为探测异常，保留全部 %d 个"
                  % (len(alive), total_raw), file=sys.stderr)

    # ---- 2) 现场测速 + 排序
    # 先用历史记录垫底（没有历史就是 0），再按 ping 升序取前 10 个现场测真实吞吐。
    # ping 与吞吐正相关（实测 ping 4s 的节点吞吐只有 ping 2.2s 的一半），
    # 所以按 ping 挑候选能覆盖大部分快节点，不必全量测。
    bench = load_bench(root)
    for o in outs:
        o["_kbps"] = bench.get(o["_address"], 0.0)
    outs.sort(key=lambda o: (o.get("_ping") or 99999))
    if xray and do_speed:
        live = probe_throughput(outs, xray, top_n=max(10, FAST_N * 3))
        if len(live) >= 2:
            for o in outs:
                if o["_address"] in live:
                    o["_kbps"] = live[o["_address"]]
            top = max(live.values())
            med = sorted(live.values())[len(live) // 2]
            print("INFO 现场测速：%d 个节点出数据，最快 %.0f KB/s，中位 %.0f KB/s"
                  % (len(live), top, med), file=sys.stderr)
        else:
            print("WARN 现场测速只拿到 %d 个结果，退回按历史数据排序" % len(live),
                  file=sys.stderr)
    outs.sort(key=lambda o: (-o["_kbps"], o.get("_ping") or 99999))

    # ---- 3) 分组：fast 组（全局最快）+ 站点国家组
    # 为什么必须分组：leastPing 只在「延迟」上择优，实测这份订阅里最快的节点 640 KB/s、
    # 最慢的只有 180 KB/s，但两者延迟可能都在 1s 上下。不做分组，balancer 会在快慢节点
    # 之间随机跳 —— 用户感受就是「同一分钟里一下快一下卡死」。
    # 把最快的几个单独打上 proxy-fast- 前缀、主线路只在这个小池子里挑，速度才稳定。
    need_regions = {r["region"] for r in rules_for_region}
    fast_n = min(FAST_N, len(outs)) if FAST_N > 0 else 0
    fast = outs[:fast_n]
    rest = outs[fast_n:]
    for i, o in enumerate(fast, 1):
        o["tag"] = "proxy-fast-%d" % i

    keep: list[dict] = list(fast)
    keep_addrs = {o["_address"] for o in fast}
    for reg in sorted(need_regions):
        cnt = 0
        for o in rest:
            if o["_region"] == reg and cnt < 4 and o["_address"] not in keep_addrs:
                keep.append(o)
                keep_addrs.add(o["_address"])
                cnt += 1
    if max_nodes:
        for o in rest:
            if len(keep) >= max_nodes:
                break
            if o["_address"] not in keep_addrs:
                keep.append(o)
                keep_addrs.add(o["_address"])
    print("INFO 分组：快速组 %d 个（主线路只用这些）+ 站点国家组，合计 %d 个"
          % (len(fast), len(keep)), file=sys.stderr)
    for o in fast:
        print("       ★ %-16s %-3s 实测 %6.0f KB/s"
              % (o["_address"], o["_region"], o["_kbps"]), file=sys.stderr)
    outs = keep

    by_region: dict[str, list[dict]] = {}
    for o in outs:
        by_region.setdefault(o["_region"], []).append(o)

    cache = read_cache(root)
    cache_dirty = False
    balancers: list[dict] = [{
        "tag": "balancer",
        "selector": ["proxy-fast-"] if fast else ["proxy-"],
        "strategy": {"type": "leastPing"},
    }]
    route_rules: list[dict] = []

    fast_addrs = {o["_address"] for o in fast}
    for r in rules_for_region:
        region = r["region"]
        # fast 组的节点已经在主线路里服役，不能改 tag（改了主线路就少一条腿），
        # 所以站点国家池只在「非 fast」节点里挑。
        pool = [o for o in by_region.get(region) or []
                if o["_address"] not in fast_addrs]
        if not pool:
            print("WARN %s 没有可独立的节点（都在快速组里），站点规则跳过" % region,
                  file=sys.stderr)
            continue

        key = cache_key(sub_url, region)
        hit = cache.get(key) or {}
        fresh = (time.time() - float(hit.get("stamp", 0))) < PROBE_TTL
        ok_set: set[str] = set(hit.get("ok") or [])
        if do_probe and xray and (not fresh or not ok_set):
            new_ok = probe_working(pool, r["probe"], xray)
            if new_ok:
                ok_set = new_ok
                cache[key] = {"stamp": time.time(), "probe": r["probe"],
                              "ok": sorted(ok_set)}
                cache_dirty = True

        # 能在该国节点里挑出「实测可通」的，就只挂这些；一个都没有就退回该国全部节点
        tagged = [o for o in pool if o["_address"] in ok_set]
        if tagged:
            for i, o in enumerate(tagged, 1):
                o["tag"] = "proxy-%s-pick-%d" % (region, i)
            bal = {"tag": "balancer-%s" % region,
                   "selector": ["proxy-%s-pick-" % region],
                   "strategy": {"type": "leastPing"}}
            hits = len(tagged)
        else:
            bal = {"tag": "balancer-%s" % region, "selector": ["proxy-%s-" % region],
                   "strategy": {"type": "leastPing"}}
            hits = len(pool)

        balancers.append(bal)
        for d in r["domains"]:
            route_rules.append({"type": "field", "network": "tcp,udp",
                                "domain": ["domain:" + d], "balancerTag": bal["tag"]})
        print("INFO %s 站点 %s -> %s（可用节点 %d/%d）"
              % (region, ",".join(r["domains"]), bal["tag"], hits, len(pool)),
              file=sys.stderr)

    if cache_dirty:
        write_cache(root, cache)

    cfg = {
        "log": {
            "loglevel": "warning",
            "access": "none",
            "error": os.path.join(root, "xray-core.log"),
        },
        "inbounds": [
            {"tag": "http-in", "listen": "127.0.0.1", "port": http_port,
             "protocol": "http", "settings": {"timeout": 0}},
            {"tag": "socks-in", "listen": "127.0.0.1", "port": socks_port,
             "protocol": "socks",
             "settings": {"auth": "noauth", "udp": True, "ip": "127.0.0.1"}},
        ],
        "outbounds": [_strip_private(o) for o in outs] + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "observatory": {
            "subjectSelector": ["proxy-"],
            "probeUrl": "https://www.gstatic.com/generate_204",
            # 60s 足够：节点已被构建期探活筛过一轮，探太勤只是白白消耗连接和流量
            "probeInterval": "60s",
            "enableConcurrency": True,
        },
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": balancers,
            "rules": [
                {"type": "field", "outboundTag": "direct",
                 "ip": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
                        "192.168.0.0/16", "169.254.0.0/16", "::1/128"]},
            ] + route_rules + [
                {"type": "field", "balancerTag": "balancer", "network": "tcp,udp"},
            ],
        },
    }

    path = out or os.path.join(root, "xray-runtime.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # xray 不认带 BOM 的 JSON
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    meta = {"nodes": len(outs), "regions": {k: len(v) for k, v in by_region.items()},
            "site_rules": len(route_rules)}
    return path, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "source", "profile-b"),
        help="这条线路的数据目录（订阅缓存、节点池、规则文件都在这儿）")
    ap.add_argument("--sub-url", default="",
                    help="订阅地址（带 token，**只在你自己的机器上用**，不要提交进仓库）")
    ap.add_argument("--http-port", type=int, default=0,
                    help="http 入口端口；0 = 随机挑一对空闲端口（推荐，避免撞端口）")
    ap.add_argument("--socks-port", type=int, default=0,
                    help="socks 入口端口；0 = 用 http 端口 +1")
    ap.add_argument("--no-probe", action="store_true", help="跳过站点探测，直接用该国全部节点")
    ap.add_argument("--no-screen", action="store_true", help="跳过构建期探活（不剔除死节点）")
    ap.add_argument("--no-speed", action="store_true", help="跳过现场测速（只按历史数据排序）")
    ap.add_argument("--max-nodes", type=int, default=10,
                    help="最多保留多少个节点（按实测吞吐排序，0=不限）")
    ap.add_argument("--out", default="",
                    help="生成的 xray 配置路径（默认 <root>/xray-runtime.json）")
    a = ap.parse_args()

    sub = a.sub_url
    xray = ""
    if not sub:
        try:
            with io.open(os.path.join(a.root, "chrome-proxy.config.json"), encoding="utf-8") as f:
                c = json.load(f)
            sub = c.get("subscriptionUrl") or DEFAULT_SUB
            xray = c.get("xrayPath") or ""
        except Exception:
            sub = DEFAULT_SUB
    if not sub:
        print("缺少订阅地址：请用 --sub-url \"<你的订阅地址>\" 传入，"
              "或写到 %s\\chrome-proxy.config.json 的 subscriptionUrl 字段。"
              % a.root, file=sys.stderr)
        return 2

    os.makedirs(a.root, exist_ok=True)
    try:
        # 端口没指定就现随机一对（不再默认 10898/10899）
        http_port, socks_port = resolve_ports(a.http_port, a.socks_port)
        path, meta = build(a.root, sub, http_port, socks_port,
                           do_probe=not a.no_probe, xray=xray,
                           do_screen=not a.no_screen, max_nodes=a.max_nodes,
                           do_speed=not a.no_speed, out=a.out)
    except Exception as e:
        print("FAIL %s" % e)
        return 1
    print("OK %s  nodes=%d  regions=%s  site_rules=%d  http=%d  socks=%d"
          % (path, meta["nodes"], meta["regions"], meta["site_rules"], http_port, socks_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
