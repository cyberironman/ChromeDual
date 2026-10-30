// Chrome「直连」模式分流规则
// 作用：网页一律 DIRECT（直连），只有 Google 账号/同步相关域名走本地代理。
// 用法：chrome.exe --proxy-pac-url="file:///<数据目录>/var/runtime/pac/split.pac"
//       （这是模板；运行时会按本次随机端口渲染到 var/runtime/pac/ 再被使用）
// 说明：端口用占位符，由管家启动时按本次随机分配的值渲染。
//       若改了端口，这里要同步改。

function FindProxyForURL(url, host) {
    var PROXY = "PROXY 127.0.0.1:__PORT_VIDEO__";
    var DIRECT = "DIRECT";

    // 本机/局域网地址永远直连
    if (isPlainHostName(host) ||
        shExpMatch(host, "127.*") ||
        shExpMatch(host, "10.*") ||
        shExpMatch(host, "192.168.*") ||
        shExpMatch(host, "172.16.*") ||
        shExpMatch(host, "localhost")) {
        return DIRECT;
    }

    // 只让这些域名走代理：Google 账号登录 + 同步 + 登录页所需静态资源 + 头像
    var proxyHosts = [
        "accounts.google.com",
        "accounts.youtube.com",
        "signin.google.com",
        "myaccount.google.com",
        "clients4.google.com",
        "clients6.google.com",
        "ogs.google.com",
        "*.googleapis.com",
        "*.gstatic.com",
        "*.googleusercontent.com"
    ];

    host = host.toLowerCase();

    for (var i = 0; i < proxyHosts.length; i++) {
        var p = proxyHosts[i];
        if (p.indexOf("*.") === 0) {
            // 通配：匹配 example.com 及其所有子域
            var base = p.substring(2);
            var suffix = "." + base;
            if (host === base) return PROXY;
            if (host.length > suffix.length &&
                host.substring(host.length - suffix.length) === suffix) {
                return PROXY;
            }
        } else if (host === p) {
            return PROXY;
        }
    }

    return DIRECT;
}
