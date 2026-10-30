// ============================================================
// Chrome「分流」模式 PAC —— 供系统默认浏览器 / 「直连」线路使用
// 文件：assets/pac/system.pac
// 服务：http://127.0.0.1:<随机端口>/system.pac（由管家按需启动的 PAC 服务提供）
//
// 设计原则：
//   1. 默认全部直连（DIRECT）—— 国内站、局域网、本地服务都不受影响
//   2. 只有下面两份名单里的站才走代理，且两份名单的线路优先级不同：
//        · 视频名单（YouTube 系）→ 优先「视频线路」
//        · 其余境外站            → 优先「默认线路」
//      这两条线路具体是哪一条，由 config/profiles.json 的
//      pac_server.video_line / pac_server.default_line 决定，与本文件无关。
//   3. 每条链都带兜底：选中线路挂了自动换另一条，两条都挂才退直连
//
// 端口说明：下面的端口是占位符 __PORT_VIDEO__ / __PORT_DEFAULT__，
//   真实值由管家在启动 PAC 服务前渲染（app/pac.py：render）。
//   端口是随机分配的，**不能**写死数字，否则 PAC 会指向不存在的端口。
// ============================================================

function FindProxyForURL(url, host) {
    // ⚠ 端口占位符：真实值由管家在启动 PAC 服务前渲染（app/pac.py：render）。
    //   端口是随机分配的，这里**不能**写死数字，否则 PAC 会指向不存在的端口。
    var PROXY_VIDEO = "PROXY 127.0.0.1:__PORT_VIDEO__; PROXY 127.0.0.1:__PORT_DEFAULT__; DIRECT";
    var PROXY_DEFAULT = "PROXY 127.0.0.1:__PORT_DEFAULT__; PROXY 127.0.0.1:__PORT_VIDEO__; DIRECT";
    var DIRECT = "DIRECT";

    host = host.toLowerCase();

    // ---- 1. 本机 / 局域网 / 无点主机名：永远直连 ----
    if (isPlainHostName(host) ||
        host === "localhost" ||
        host === "127.0.0.1" ||
        host === "[::1]" ||
        host.indexOf("127.") === 0 ||
        host.indexOf("10.") === 0 ||
        host.indexOf("192.168.") === 0 ||
        host.indexOf("169.254.") === 0 ||
        /^172\.(1[6-9]|2[0-9]|3[01])\./.test(host) ||
        host.indexOf(".local") > 0 ||
        host.indexOf(".lan") > 0) {
        return DIRECT;
    }

    // ---- 2. 视频名单：优先「视频线路」，看视频通常更顺 ----
    var videoDomains = [
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "ytimg.com",
        "googlevideo.com",
        "ggpht.com",
        "yt.be"
    ];

    for (var i = 0; i < videoDomains.length; i++) {
        if (hostMatches(host, videoDomains[i])) {
            return PROXY_VIDEO;
        }
    }

    // ---- 3. 其余需要走代理的域名：优先「默认线路」 ----
    var proxyDomains = [
        // Google 全家桶（不含 YouTube 系，已被上面拦走）
        "google.com", "google.com.hk", "google.com.tw", "google.co.jp",
        "googleapis.com", "gstatic.com", "googleusercontent.com",
        "gvt1.com", "withgoogle.com", "recaptcha.net", "gmail.com",
        "blogger.com", "blogspot.com", "goo.gl",

        // 其他视频/音频（非 YouTube，走默认线路）
        "netflix.com", "nflxvideo.net", "twitch.tv", "vimeo.com",
        "soundcloud.com", "spotify.com", "scdn.co",

        // AI / 开发
        "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
        "anthropic.com", "claude.ai", "perplexity.ai", "huggingface.co",
        "poe.com", "cursor.com", "midjourney.com",
        "docker.com", "docker.io", "registry-1.docker.io",
        "raw.githubusercontent.com", "gist.github.com",
        "npmjs.org", "pypi.org",

        // 社交 / 内容
        "twitter.com", "x.com", "t.co", "facebook.com", "fb.com",
        "facebook.net", "instagram.com", "cdninstagram.com",
        "whatsapp.com", "whatsapp.net", "telegram.org", "t.me",
        "telesco.pe", "reddit.com", "redd.it", "discord.com",
        "discordapp.com", "discord.gg", "medium.com", "quora.com",
        "tumblr.com", "pinterest.com", "linkedin.com", "patreon.com",
        "substack.com", "threads.net",

        // 百科 / 资讯
        "wikipedia.org", "wikimedia.org", "wiktionary.org",
        "nytimes.com", "wsj.com", "bloomberg.com", "economist.com",
        "reuters.com", "bbc.com", "bbc.co.uk", "cnn.com",
        "theguardian.com", "ft.com", "apnews.com", "voanews.com",
        "rfi.fr", "dw.com", "nikkei.com", "asahi.com",

        // 工具 / 云服务
        "dropbox.com", "dropboxusercontent.com",
        "archive.org", "duckduckgo.com", "stackoverflow.com",
        "cloudflare.com", "workers.dev", "pages.dev",
        "vercel.app", "netlify.app", "herokuapp.com",
        "amazonaws.com", "awsstatic.com", "bing.com",
        "notion.so", "figma.com", "canva.com", "slack.com",
        "zoom.us", "okta.com"
    ];

    for (var i = 0; i < proxyDomains.length; i++) {
        if (hostMatches(host, proxyDomains[i])) {
            return PROXY_DEFAULT;
        }
    }

    // ---- 4. 其余一律直连 ----
    return DIRECT;
}

// 精确匹配 + 子域匹配（example.com 也匹配 a.example.com）
function hostMatches(host, domain) {
    if (host === domain) {
        return true;
    }
    var suffix = "." + domain;
    if (host.length > suffix.length &&
        host.substring(host.length - suffix.length) === suffix) {
        return true;
    }
    return false;
}
